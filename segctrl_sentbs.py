import os
# below commented off, add in execution script
# os.environ["CUDA_VISIBLE_DEVICES"]="0" # need to appear before importing torch modules; if changed, need to restart kernel
os.environ["CUDA_LAUNCH_BLOCKING"]="1"

from typing import Tuple, List, Optional, Union
import argparse
from tqdm import tqdm
from termcolor import colored
import math 

import pandas as pd
import torch

from transformers.tokenization_utils_base import BatchEncoding

from transformers import (
    set_seed,
    PreTrainedModel,
    PreTrainedTokenizerFast,
    AutoConfig,
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    BeamSearchScorer,
    AutoTokenizer,
    AutoModelForSequenceClassification
)

from transformers.generation_stopping_criteria import (
    StoppingCriteriaList,
)
from stopping_criteria import ( # self-defined
    EndSentenceCriteria,
    EndSpanCriteria,
    MultiBatchEndSentenceCriteria,
)


from beam_search_sent_utils import (
    sample,
    beam_search,
    beam_sample,
)

from proto import GenerationItem

from datasets import load_metric, load_dataset, load_from_disk
import numpy as np

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
print("device:", device)

import argparse

# from utils import remove_prompts

def parse_arguments(parser):
    parser.add_argument('--classifier_device', type=str, default="cuda:0", choices=['cpu', 'cuda:0', 'cuda:1', 'cuda:2','cuda:3'], help="GPU/CPU devices")
    parser.add_argument('--generation_model_path', type=str, default="", help="the path of the generation model to be loaded")
    parser.add_argument('--classification_model_path', type=str, default="", help="the path of the classification model to be loaded")
    parser.add_argument('--test_file', type=str, default="", help="the path of the test file")
    parser.add_argument('--res_dir', type=str, default="", help="the directory to store generated text")
    parser.add_argument('--dataset_name', type=str, default="mred", help="name of the dataset used")
    parser.add_argument('--dataset_path', type=str, default="", help="path to pre-downloaded huggingface datasets")
    parser.add_argument('--write_mode', type=str, default="w", choices=['w', 'a'], help="whether to over-write or append to file")
    parser.add_argument('--gen_mode', type=str, default="beam_search_sent", choices=['beam_search_sent', 'greedy_search','beam_search','beam_sample','sample', 'beam_search_span'], help="whether to over-write or append to file")
    parser.add_argument('--res_file_name', type=str, default="auto", help="where to store txt file, whether use auto generated name")
    
    parser.add_argument('--test_start_idx', type=int, default=0, help="the test example idx to start evaluation from")
    parser.add_argument('--top_p', type=float, default=0.9, help="the cutoff p value for neucleus sampling")
    parser.add_argument('--run_num', type=int, default=0, help="the seed to use")
    parser.add_argument('--gen_size', type=int, default=8, help="number of sentence options to generate")
    parser.add_argument('--beam_size', type=int, default=4, help="number of sentence options to keep for next round of generation")
    parser.add_argument('--gen_target_max', type=int, default=800, help="maximum number of token ids allowed for the target")
    parser.add_argument('--max_source_length', type=int, default=2048, help="maximum number of token ids allowed for the source before truncation")
    parser.add_argument('--stop_by_tokens', type=int, default=20, help="number of tokens to generation each time for stopping criteria")
    parser.add_argument('--bs_num_beams', type=int, default=4, help="num beams for beam search (the original version)")
    parser.add_argument('--max_batch_size', type=int, default=4, help="maximum batch size used during decoding process")

    parser.add_argument('--write', action="store_true", default=False, help="Whether to write output")
    parser.add_argument('--load_classifier', action="store_true", default=False, help="Whether to load classifier model")
    parser.add_argument('--debug', action="store_true", default=False, help="Whether in debug mode")
    parser.add_argument('--eval_rouge', action="store_true", default=False, help="Whether in evaluate rouge on the go")
    parser.add_argument('--beam_sample', action="store_true", default=False, help="Whether to use beam sampling for nucleus sampling")

    args = parser.parse_args()
    for k in args.__dict__:
        print(k + ": " + str(args.__dict__[k]))
    return args

parser = argparse.ArgumentParser()
args = parse_arguments(parser)
# --------- Parameters ---------
assert args.beam_size <= (args.gen_size * args.beam_size) # if larger, we are not filtering and this makes little sense ...
GEN_SIZE = args.gen_size # total number of sentence options to generate
BEAM_SIZE = args.beam_size # number of sentences to keep from the options
MAX_TARGET_LENGTH = args.gen_target_max
BS_NUM_BEAMS = args.bs_num_beams
TOP_P = args.top_p
classifier_device = args.classifier_device
model_path=args.generation_model_path
classfication_model_path = args.classification_model_path
config = AutoConfig.from_pretrained(model_path)
config.gen_target_max = args.gen_target_max
config.max_position_embeddings = args.max_source_length
test_file = args.test_file
write_mode = args.write_mode
test_start_idx = args.test_start_idx
gen_mode = args.gen_mode

worst_score = 1e-6 # lowest score allowed 

if not os.path.exists(args.res_dir):
    os.mkdir(args.res_dir)

if args.write:
    if args.res_file_name == "auto":    
        result_file_path = os.path.join(args.res_dir, args.dataset_name+"-gen_size_"+str(GEN_SIZE)+"-beam_size_"+str(BEAM_SIZE)+"-top_p_"+str(TOP_P)+"-"+str(args.run_num)+".txt")
    else:
        result_file_path =  os.path.join(args.res_dir, args.res_file_name)
    rouge_file_path = result_file_path.replace(".txt", ".rouge")

    if os.path.exists(result_file_path) and write_mode == "w":
        print(colored(f"please rename result file path to avoid it being over-written: {result_file_path}", 'red'))
    else:
        print("result_file_path:", result_file_path)
        print("rouge_file_path:", rouge_file_path)
        if write_mode == "a":
            print("appending results to previous content")
else: 
    print(colored(f"generation results will not be saved, you may want to add --write to save result to some file", 'red'))


if gen_mode == "beam_search_span":
    print(colored(f"generation per {args.stop_by_tokens} tokens", 'green'))

labels2idx={
    "abstract":0,
    "strength":1, 
    "weakness":2, 
    "suggestion":3, 
    "ac_disagreement":4, 
    "rebuttal_process":5,
    "rating_summary":6, 
    "decision":7, 
    "misc":8
}

set_seed(args.run_num)

# --------- Read Test File --------------
if test_file[-3:] == "csv":
    df_test = pd.read_csv(test_file)
    df_test = df_test[['text', 'summary']]
    text_list = df_test["text"].tolist()
    target_list = df_test["summary"].tolist()
    total_test_examples = len(text_list)
else:
    text_list = None
    raw_datasets = load_from_disk(args.dataset_path)
    total_test_examples = len(raw_datasets)

print("total test examples in test file:",total_test_examples)
    

# --------- Load Generation Model ---------

model = AutoModelForSeq2SeqLM.from_pretrained(model_path,config=config).to(device)
tokenizer = AutoTokenizer.from_pretrained(model_path,use_fast=True) 
model.resize_token_embeddings(len(tokenizer))
model.eval()
# NOTE: here using my self-defined sample function to override what is defined in generation_utils
model.sample = sample.__get__(model)

# TODO: write customized function rather than replacing the original !!!
if gen_mode == "beam_search_sent": 
    model.beam_search = beam_search.__get__(model)

model.beam_sample = beam_sample.__get__(model)
model.tokenizer = tokenizer
length_penalty = model.config.length_penalty

if args.load_classifier:
    # --------- Load Classifier Model ---------
    num_labels=len(labels2idx.keys())
    classification_model = AutoModelForSequenceClassification.from_pretrained(classfication_model_path, num_labels=num_labels).to(device)
    classification_tokenizer = AutoTokenizer.from_pretrained(classfication_model_path, use_fast=True) 
    classification_model.eval()

    # --------- Classification Functions ---------
    def get_classification_logprob(model, tokenizer, text, target_labels, allowed_positions):
        """
        returns a tuple of:
            logprob: log probability of how likely the sentence belongs to the given class options
            target_labels: list of target label ids 
            allowed_positions: set of idx positions in the target_labels that are possible class options
        """
        # NOTE: roberta only accepts up to 512 tokens
        input_ids = tokenizer(text).input_ids
        logits = model(torch.LongTensor([[input_ids[0]]+input_ids[1:-1][:510]+[input_ids[-1]]]).to(device)).logits.detach()
        logprob = torch.nn.functional.log_softmax(logits, dim=-1)[0]
        # indices = torch.sort(logprob, descending=True).indices
        # rank = (indices ==label_idx).nonzero().squeeze().item()
        classification_score = None 
        curr_label_idx = None

        for pos in allowed_positions:
            label = target_labels[pos]
            curr_logprob = logprob[label].item()
            curr_label_idx = pos if classification_score is None or classification_score < curr_logprob else curr_label_idx
            classification_score = curr_logprob if classification_score is None or classification_score < curr_logprob else classification_score

        return (classification_score, curr_label_idx)

# # --------- Generation Functions ---------
def process_generation(outputs, target_labels, prev_gen: Optional[GenerationItem] = None):
    
    # create set of allowed_positions
    if prev_gen is None:
        allowed_positions = {0}
    else:
        curr_pos = prev_gen.curr_label_idx
        next_pos = curr_pos+1 if curr_pos+1 < len(target_labels) else curr_pos
        allowed_positions = {curr_pos, next_pos}

    # -------- previous generation info ----------
    prev_gen_num_tokens = prev_gen.num_tokens_generated if prev_gen is not None else 0
    prev_gen_logsum = prev_gen.logsum if prev_gen is not None else 0
    prev_gen_text = prev_gen.text if prev_gen is not None else ""
    # -------- get classification probability ----------
    new_sent = tokenizer.decode(outputs.sequences[0, -len(outputs.scores):], skip_special_tokens=True)
    classification_score, curr_label_idx = get_classification_logprob(classification_model,classification_tokenizer, new_sent, target_labels, allowed_positions)
    # -------- get logsum of samples ------------
    # stack the logits generated at each step to a tensor and transform logits to probs
    probs = torch.stack(outputs.scores, dim=1).softmax(-1)  # -> shape [num_seq, seq_len, vocab_size]
    # NOTE: only evaluate for the last sentence
    gen_sequence = outputs.sequences[:, -len(outputs.scores):]  # -> shape [num_seq, seq_len]
    # collect the probability of the generated token, need to add a dummy dim in the end to make gather work
    gen_probs = torch.gather(probs, 2, gen_sequence[:, :, None]).squeeze(-1)  # -> shape [num_seq, seq_len]  
    # add log probability up, ignore places where padding is used (aka. where id is 1)
    mask = (outputs.sequences==tokenizer.pad_token_id)[:, -len(outputs.scores):] 
    gen_probs.masked_fill_(mask, 1) # replace pad token with prob of 1, so log prob will be 0
    # NOTE: need to get average score, otherwise we are biased towards shortsequences
    logsum = torch.sum(torch.log(gen_probs),1) + prev_gen_logsum
    # --------- get decoded text ---------------
    # text = tokenizer.decode(outputs.sequences[0], skip_special_tokens=True)
    text = " ".join([prev_gen_text.strip(), new_sent.strip()]) 
    num_tokens_generated = prev_gen_num_tokens + len(outputs.scores)     

    return GenerationItem(outputs.sequences, logsum, classification_score, text, num_tokens_generated, curr_label_idx=curr_label_idx)

def process_beamsearch_generation(beamsearch_outputs, target_labels, start_pos, prev_gen: Optional[GenerationItem] = None):
    """
    return:
        tuple (GenerationItem, beamsearch_stopped)
        if no additional sentence is generated, returned GenerationItem is None, beamsearch_stopped is True 
    """
    # NOTE: 2 dim, with first dim of size 1 in order to work
    assert beamsearch_outputs.sequences.size(0) == 1 and beamsearch_outputs.sequences.dim() == 2
    # cut off pad ids
    pad_mask = beamsearch_outputs.sequences==tokenizer.pad_token_id
    eos_mask = beamsearch_outputs.sequences==tokenizer.eos_token_id
    comb_mask = pad_mask.logical_or(eos_mask)
    # assert comb_mask.size(0) == 1 and comb_mask.dim() == 2
    last_valid_idx = (comb_mask == False).nonzero()[-1][1].item() 
    curr_gen_ids = beamsearch_outputs.sequences[:,:last_valid_idx+1]

    # create set of allowed_positions
    if prev_gen is None:
        allowed_positions = {0}
    else:
        curr_pos = prev_gen.curr_label_idx
        next_pos = curr_pos+1 if curr_pos+1 < len(target_labels) else curr_pos
        allowed_positions = {curr_pos, next_pos}
            
    # only add to generations if new sentence generated
    # future beam search will be skipped if no new sentence generated from previous beam search
    # if prev_gen is None or curr_gen_ids.size(1) > prev_gen.token_ids.size(1): 
    if prev_gen is None or curr_gen_ids.size(1) > start_pos: 
        # get scores
        # start_pos = 1 if prev_gen is None else prev_gen.token_ids.size(1)
        end_pos = last_valid_idx+1 # later put pad token probability to be 1 
        gen_ids = beamsearch_outputs.sequences[:, start_pos:end_pos][0]
        # print("gen_ids", gen_ids)
        num_tokens_generated = end_pos - start_pos
        # print("start - end:", start_pos, end_pos)
        # print("num tokens generated:", num_tokens_generated)
        probs = torch.stack(beamsearch_outputs.scores, dim=0).softmax(-1)[:num_tokens_generated] # size [seq_len, num_beams, vocab_size]
        beam_indices = torch.stack(beamsearch_outputs.beam_indices[0], dim=0)[:num_tokens_generated] # size [seq_len]
        gen_probs = torch.stack([probs[pos, beam_idx, vocab_idx] for pos, (beam_idx, vocab_idx) in enumerate(zip(beam_indices, gen_ids))],dim=0)
        # print(gen_probs)
        logsum = torch.sum(torch.log(gen_probs)).item()
        # classification score
        new_sent = tokenizer.decode(gen_ids, skip_special_tokens=True)
        classification_score, curr_label_idx = get_classification_logprob(classification_model,classification_tokenizer, new_sent, target_labels, allowed_positions)

        # finalize value with prev_gen
        prev_gen_num_tokens = prev_gen.num_tokens_generated if prev_gen is not None else 0
        prev_gen_logsum = prev_gen.logsum if prev_gen is not None else 0
        prev_gen_text = prev_gen.text if prev_gen is not None else ""
        text = " ".join([prev_gen_text.strip(), new_sent.strip()]) 
        logsum += prev_gen_logsum
        # text = tokenizer.decode(curr_gen_ids[0], skip_special_tokens=True) # get full text directly
        num_tokens_generated += prev_gen_num_tokens
        item = GenerationItem(curr_gen_ids, logsum, classification_score, text, num_tokens_generated, beamsearch_stopped=False, curr_label_idx=curr_label_idx)
        # print("\n beamsearch gen state: logsum {} | num tokens {} | avg log {} | class prob {} | {}".format(item.logsum,item.num_tokens_generated, item.get_avg_log(), item.classification_score, item.text))
        return (item, False) # 
    else:
        return (None, True)

def process_beamsample_generation(beamsearch_outputs, target_labels, start_pos, prev_gen: Optional[GenerationItem] = None):
    """
    return:
        tuple (List[GenerationItem], beamsearch_stopped)
        if no additional sentence is generated, returned GenerationItem is None, beamsearch_stopped is True 
    """

    assert beamsearch_outputs.sequences.dim() == 2
    generations = []

    # create set of allowed_positions
    if prev_gen is None:
        allowed_positions = {0}
    else:
        curr_pos = prev_gen.curr_label_idx
        next_pos = curr_pos+1 if curr_pos+1 < len(target_labels) else curr_pos
        allowed_positions = {curr_pos, next_pos}


    for gen_idx in range(beamsearch_outputs.sequences.size(0)):
        # cut off pad ids
        pad_mask = beamsearch_outputs.sequences[gen_idx]==tokenizer.pad_token_id
        eos_mask = beamsearch_outputs.sequences[gen_idx]==tokenizer.eos_token_id
        comb_mask = pad_mask.logical_or(eos_mask)
        # assert comb_mask.size(0) == 1 and comb_mask.dim() == 2
        last_valid_idx = (comb_mask == False).nonzero()[-1].item() 
        curr_gen_ids = beamsearch_outputs.sequences[gen_idx,:last_valid_idx+1].unsqueeze(0)

        
        # only add to generations if new sentence generated
        # future beam search will be skipped if no new sentence generated from previous beam search
        if prev_gen is None or curr_gen_ids.size(1) > start_pos: 
            # get scores
            # start_pos = 1 if prev_gen is None else prev_gen.token_ids.size(1)
            end_pos = last_valid_idx+1 # later put pad token probability to be 1 
            gen_ids = beamsearch_outputs.sequences[gen_idx, start_pos:end_pos]
            num_tokens_generated = end_pos - start_pos
            # print("start - end:", start_pos, end_pos)
            # print("num tokens generated:", num_tokens_generated)
            probs = torch.stack(beamsearch_outputs.scores, dim=0).softmax(-1)[:num_tokens_generated]
            beam_indices = torch.stack(beamsearch_outputs.beam_indices[gen_idx], dim=0)[:num_tokens_generated]
            gen_probs = torch.stack([probs[pos, beam_idx, vocab_idx] for pos, (beam_idx, vocab_idx) in enumerate(zip(beam_indices, gen_ids))],dim=0)
            logsum = torch.sum(torch.log(gen_probs)).item()
            # classification score
            new_sent = tokenizer.decode(gen_ids, skip_special_tokens=True)
            classification_score, curr_label_idx = get_classification_logprob(classification_model,classification_tokenizer, new_sent, target_labels, allowed_positions)
            # finalize value with prev_gen
            prev_gen_num_tokens = prev_gen.num_tokens_generated if prev_gen is not None else 0
            prev_gen_logsum = prev_gen.logsum if prev_gen is not None else 0
            prev_gen_text = prev_gen.text if prev_gen is not None else ""
            text = " ".join([prev_gen_text.strip(), new_sent.strip()]) 
            # text = tokenizer.decode(curr_gen_ids[0], skip_special_tokens = True)
            logsum += prev_gen_logsum
            num_tokens_generated += prev_gen_num_tokens
            item = GenerationItem(curr_gen_ids, logsum, classification_score, text, num_tokens_generated, beamsearch_stopped=False, curr_label_idx=curr_label_idx)
            # print("\n beamsearch gen state: logsum {} | num tokens {} | avg log {} | class prob {} | {}".format(item.logsum,item.num_tokens_generated, item.get_avg_log(), item.classification_score, item.text))
            generations.append(item)
        # else:
        #     print("beam sample didn't generate effective items, not adding to gen:", curr_gen_ids.size(), prev_gen.token_ids.size())
        #     print("curr:", beamsearch_outputs.sequences[gen_idx])
        #     print("prev:", prev_gen.token_ids)
    return generations

def process_multisample_generation(sample_outputs, target_labels, start_pos, prev_gen: Optional[GenerationItem] = None):
    """
    return:
        List [GenerationItem]
    """
    # NOTE: sample_outputs size [num_return_sequences, seq_len]
    generations = []

    # create set of allowed_positions
    if prev_gen is None:
        allowed_positions = {0}
    else:
        curr_pos = prev_gen.curr_label_idx
        next_pos = curr_pos+1 if curr_pos+1 < len(target_labels) else curr_pos
        allowed_positions = {curr_pos, next_pos}

    # cut off pad ids
    pad_mask = sample_outputs.sequences==tokenizer.pad_token_id
    eos_mask = sample_outputs.sequences==tokenizer.eos_token_id
    comb_mask = pad_mask.logical_or(eos_mask)
    probs = torch.stack(sample_outputs.scores, dim=0).softmax(-1)

    # format each sequence into a GenerationItem
    for num_seq in range(sample_outputs.sequences.size(0)): 
        last_valid_idx = (comb_mask[num_seq] == False).nonzero()[-1].item() 
        curr_gen_ids = sample_outputs.sequences[num_seq,:last_valid_idx+1].unsqueeze(0)
        # get scores
        # start_pos = 1 if prev_gen is None else prev_gen.token_ids.size(1)
        end_pos = end_pos = last_valid_idx+1 # later put pad token probability to be 1 
        gen_ids = sample_outputs.sequences[num_seq, start_pos:end_pos]
        # print("gen_ids", gen_ids)
        num_tokens_generated = end_pos - start_pos
        curr_probs = probs[:, num_seq, :].squeeze(1)[:num_tokens_generated] # size [seq_len, num_beams, vocab_size]
        gen_probs = torch.gather(curr_probs, -1, gen_ids[:, None]).squeeze(-1)
        logsum = torch.sum(torch.log(gen_probs)).item()
        # classification score
        new_sent = tokenizer.decode(gen_ids, skip_special_tokens=True)
        classification_score, curr_label_idx = get_classification_logprob(classification_model,classification_tokenizer, new_sent, target_labels, allowed_positions)
        # finalize value with prev_gen
        prev_gen_num_tokens = prev_gen.num_tokens_generated if prev_gen is not None else 0
        prev_gen_logsum = prev_gen.logsum if prev_gen is not None else 0
        prev_gen_text = prev_gen.text if prev_gen is not None else ""
        text = " ".join([prev_gen_text.strip(), new_sent.strip()]) 
        # text = tokenizer.decode(curr_gen_ids[0], skip_special_tokens=True)
        logsum += prev_gen_logsum
        num_tokens_generated += prev_gen_num_tokens
        item = GenerationItem(curr_gen_ids, logsum, classification_score, text, num_tokens_generated, curr_label_idx = curr_label_idx)
        generations.append(item)
        # print("\n multibatch sample state: logsum {} | num tokens {} | avg log {} | class prob {} | {}".format(item.logsum,item.num_tokens_generated, item.get_avg_log(), item.classification_score, item.text))
    
    return generations
    
def generate_sent(
    input_ids, 
    stopping_criteria, 
    max_length=MAX_TARGET_LENGTH, 
    top_p=TOP_P, 
    do_sample=True, 
    decoder_input_ids=None, 
    early_stopping=True, 
    num_return_sequences=1, 
    num_beams=1, 
    output_scores=True, 
    return_dict_in_generate=True,
    init_beam_scores = None,
):
    if decoder_input_ids is not None:
        return model.generate(
            input_ids=input_ids, 
            max_length=max_length, 
            do_sample=do_sample,
            early_stopping=early_stopping,
            top_p=top_p,
            num_return_sequences=num_return_sequences,
            num_beams=num_beams,
            output_scores=output_scores,
            return_dict_in_generate=return_dict_in_generate,
            stopping_criteria=stopping_criteria,
            decoder_input_ids=decoder_input_ids,
            gen_mode = args.gen_mode, # pass this to customized generation kwargs
            init_beam_scores = init_beam_scores,
        )
    else: 
        return model.generate(
            input_ids=input_ids, 
            max_length=max_length, 
            do_sample=do_sample,
            early_stopping=early_stopping,
            top_p=top_p,
            num_return_sequences=num_return_sequences,
            num_beams=num_beams,
            output_scores=output_scores,
            return_dict_in_generate=return_dict_in_generate,
            stopping_criteria=stopping_criteria,
            gen_mode = args.gen_mode, # pass this to customized generation kwargs
            init_beam_scores = init_beam_scores,
        )

def generate_sentence_options(
    sample_size: int, 
    # num_sents: int, 
    input_ids: torch.LongTensor, 
    target_labels: List[int],
    # decoder_input_ids: Optional[torch.LongTensor] = None,
    prev_gen: Optional[GenerationItem] = None,
    prev_beamsearch_stopped: Optional[bool] = False,
    decoder_input_ids: Optional[torch.LongTensor] = None,
):
    """
        sample_size: number of sentences to generate from sampling
        num_sents: number of new sentences to generate
        input_ids: input_ids from source
        prev_gen: previously generated sentence class
        target_labels: the list of target labels idx
        decoder_input_ids: directly specify the decoder input ids if using ITSP, containing added prompt tokens
    """
    beamsearch_stopped = False # flag this once beam search cannot generate anymore sentences
    generations = []

    stopping_criteria = StoppingCriteriaList()
    stopping_criteria.append(EndSentenceCriteria(tokenizer=tokenizer))
    multibatch_stopping_criteria = StoppingCriteriaList()
    multibatch_stopping_criteria.append(MultiBatchEndSentenceCriteria(tokenizer.pad_token_id))
    decoder_input_ids= decoder_input_ids if decoder_input_ids is not None else prev_gen.token_ids if prev_gen is not None else None
    decoder_input_id_length = decoder_input_ids.size(1) if decoder_input_ids is not None else 0
    start_pos = decoder_input_ids.size(-1) if decoder_input_ids is not None else prev_gen.token_ids.size(-1) if prev_gen is not None else 1

    if decoder_input_id_length >= MAX_TARGET_LENGTH: # no need to generate further if exceed max length
        item = prev_gen
        item.classification_score = 0
        generations.append(item)
        print(colored(f"generation force stopped due to exceeding max length, you may consider use longer MAX_TARGET_LENGTH", 'red'))

    else:
        if prev_gen is None or not prev_gen.beamsearch_stopped:
            # beam search
            beamsearch_outputs = generate_sent(
                input_ids, 
                multibatch_stopping_criteria,
                do_sample=False,
                num_beams= BS_NUM_BEAMS,
                decoder_input_ids=decoder_input_ids,
            )
            item, beamsearch_stopped = process_beamsearch_generation(beamsearch_outputs, target_labels, start_pos, prev_gen = prev_gen)
            if beamsearch_stopped or (prev_gen is not None and prev_gen.curr_label_idx+1 == len(target_labels) and item.classification_score < -5):
                # if last label sentence already generated and the new sentence classification probs is too low
                generations.append(prev_gen)
                return (generations, True) # if beam search stop, don't use other methods, just stop
            else:
                generations.append(item)


        if args.beam_sample:
            # beam sampling 
            beamsample_outputs = generate_sent(
                input_ids, 
                multibatch_stopping_criteria,
                top_p=TOP_P,
                num_beams=BS_NUM_BEAMS,
                do_sample=True,
                num_return_sequences=min((sample_size - len(generations)), 4),
                decoder_input_ids=decoder_input_ids,
            )
            items = process_beamsample_generation(beamsample_outputs, target_labels, start_pos, prev_gen=prev_gen)
            generations.extend(items)

        # neucleus sampling
        sample_outputs = generate_sent(
            input_ids, 
            multibatch_stopping_criteria,
            top_p=TOP_P,
            num_beams=1,
            num_return_sequences=sample_size - len(generations),
            decoder_input_ids=decoder_input_ids,
        )
        items = process_multisample_generation(sample_outputs, target_labels, start_pos, prev_gen = prev_gen)
        generations.extend(items)

    return (generations, beamsearch_stopped)

def generate_beamsample_options(
    sample_size: int, 
    input_ids: torch.LongTensor, 
    target_label: int,
    prev_gen: Optional[GenerationItem] = None,
    decoder_input_ids: Optional[torch.LongTensor] = None,
):
    """
        sample_size: number of sentences to generate from sampling
        num_sents: number of new sentences to generate
        input_ids: input_ids from source
        prev_gen: previously generated sentence class
        target_label: the idx for the intended generation
    """
    generations = []
    multibatch_stopping_criteria = StoppingCriteriaList()
    multibatch_stopping_criteria.append(MultiBatchEndSentenceCriteria(tokenizer.pad_token_id))
    decoder_input_ids= decoder_input_ids if decoder_input_ids is not None else prev_gen.token_ids if prev_gen is not None else None
    decoder_input_id_length = decoder_input_ids.size(1) if decoder_input_ids is not None else 0
    start_pos = decoder_input_ids.size(-1) if decoder_input_ids is not None else prev_gen.token_ids.size(-1) if prev_gen is not None else 1


    if decoder_input_id_length >= MAX_TARGET_LENGTH: # no need to generate further if exceed max length
        item = prev_gen
        item.classification_score = 0
        generations.append(item)
        print(colored(f"generation force stopped due to exceeding max length, you may consider use longer MAX_TARGET_LENGTH", 'red'))

    else:
        # beam sampling 
        beamsample_outputs = generate_sent(
            input_ids, 
            multibatch_stopping_criteria,
            top_p=TOP_P,
            num_beams=sample_size,
            do_sample=True,
            num_return_sequences=sample_size,
            decoder_input_ids=decoder_input_ids,
        )
        items = process_beamsample_generation(beamsample_outputs, target_label, start_pos, prev_gen=prev_gen)
        generations.extend(items)
    return generations

def generte_sample_options(
    sample_size: int, 
    input_ids: torch.LongTensor, 
    target_label: int,
    prev_gen: Optional[GenerationItem] = None,
):
    # neucleus sampling
    decoder_input_id_length = prev_gen.token_ids.size(1) if prev_gen is not None else 0
    if decoder_input_id_length >= MAX_TARGET_LENGTH: # no need to generate further if exceed max length
        item = prev_gen
        item.classification_score = 0
        return [item]
        print(colored(f"generation force stopped due to exceeding max length, you may consider use longer MAX_TARGET_LENGTH", 'red'))

    multibatch_stopping_criteria = StoppingCriteriaList()
    multibatch_stopping_criteria.append(MultiBatchEndSentenceCriteria(tokenizer.pad_token_id))
    sample_outputs = generate_sent(
        input_ids, 
        multibatch_stopping_criteria,
        top_p=TOP_P,
        num_beams=1,
        num_return_sequences=sample_size,
        decoder_input_ids=prev_gen.token_ids if prev_gen is not None else None,
    )
    items = process_multisample_generation(sample_outputs, target_label, prev_gen)
    return items

def sort_filter_gen_history(sent_options:List[GenerationItem], n:int): # n is the number of top sentences to select
    return sorted(sent_options, key=lambda item: (item.get_avg_log()+item.classification_score), reverse=True)[:n] # sort in descending order

def sort_filter_gen_history_with_length_penalty(sent_options:List[GenerationItem], n:int): # n is the number of top sentences to select
    return sorted(sent_options, key=lambda item: item.seq_score, reverse=True)[:n] # sort in descending order

def sort_filter_gen_histrory_by_rank(sent_options:List[GenerationItem], n:int):
    logsum_scores = [item.get_avg_log() for item in sent_options]
    logsum_sorted = sorted(logsum_scores, reverse=True)
    # print("sorted avg:", logsum_sorted)
    def get_combined_rank(item):
        avg = item.get_avg_log()
        if avg in logsum_sorted:
            logsum_rank = logsum_sorted.index(avg)
        else:
            print(colored(f"log sum avg not found in whole list: {avg} | {logsum_sorted}", 'red'))
   
        # logsum_rank = (logsum_sorted==item.get_avg_log()).nonzero()[0][0].item() # if multiple items have same score, take the earlier rank
        # logsum_rank = (logsum_sorted==item.get_avg_log()).nonzero().squeeze().item()
        comb_rank = logsum_rank + item.classification_rank
        return (comb_rank, item.classification_rank) # specify sorting secondary key
    sorted_items = sorted(sent_options, key=get_combined_rank, reverse=False) # the lower the score, the better the overall performance
    # for i, item in enumerate(sorted_items):
    #     print(i, ":", get_combined_rank(item), item.classification_rank, item.get_avg_log(), item.text)
    return sorted_items[:n]

def sort_filter_gen_history_with_classification_rank(sent_options:List[GenerationItem], n:int): # n is the number of top sentences to select
    # classification rank: the lower the better, reverse as compared to avg logsum
    return sorted(sent_options, key=lambda item: (item.get_avg_log()-item.classification_rank), reverse=True)[:n] # sort in descending order
    
# -------- Generation per Token Span Functions --------
def process_beam_search_span_generation(beamsearch_outputs, prev_len: Optional[int]=1):
    """
    prev_len: if batch_decoding used and not first time generation, it is the decoder_input_ids seq_len
    return:
        (GenerationList, Completed_List)
    """
    generation_list = []
    completed_list = []

    assert beamsearch_outputs.sequences.dim() == 2

    prev_ids = None
    for idx in range(beamsearch_outputs.sequences.size(0)):
        start_pos = prev_len
        eos_mask = beamsearch_outputs.sequences[idx, :]==tokenizer.eos_token_id
        pad_mask = beamsearch_outputs.sequences[idx, :]==tokenizer.pad_token_id
        comb_mask = pad_mask.logical_or(eos_mask)
        end_pos = (comb_mask == True).nonzero()[1].item() # ignore the beginning eos
        # end_pos = beamsearch_outputs.sequences.size(-1)-1 # NOTE: last eos is not generated but appended by beam search algo
        new_gen_ids = beamsearch_outputs.sequences[idx, start_pos:end_pos]

        # keep track and avoid duplicates
        if prev_ids is not None and new_gen_ids.size(-1) == prev_ids.size(-1) and (new_gen_ids == prev_ids).all().item(): 
            continue
        else: 
            prev_ids = new_gen_ids

        # check if sequence finished generation from beamsearch
        completed = False 
        if new_gen_ids.size(-1) < args.stop_by_tokens: # finished generation already
            completed = True

        # calculate logsum 
        cumulated_gen_ids = beamsearch_outputs.sequences[idx,:end_pos] #.unsqueeze(0)
        
        # there no need to add logsum word by word, since it is the accumulated beam_scores throughout all operations
        logsum = beamsearch_outputs.prev_beam_scores[idx].item()
        seqscore = beamsearch_outputs.sequences_scores[idx].item()
        text = tokenizer.decode(cumulated_gen_ids, skip_special_tokens=True) if args.debug else ""
        item = GenerationItem(cumulated_gen_ids, logsum, seq_score=seqscore, text=text)

        if not completed:
            generation_list.append(item)
            if args.debug:
                print("new gen: logsum {} | seq_score {} | {}".format(item.logsum, item.seq_score, item.text))
        else: # generation already completed
            completed_list.append(item)
            if args.debug:
                print("completed: logsum {} | seq_score {} | {}".format(item.logsum, item.seq_score, item.text))

    return generation_list, completed_list

def generate_sentence_for_beam_search_span(
    sample_size: int, 
    input_ids: torch.LongTensor, 
    stop_by_tokens: int,
    max_length: int,
    prev_gen: Optional[GenerationItem] = None,
    decoder_input_ids: Optional[torch.LongTensor]=None,
    decoder_logsums: Optional[torch.FloatTensor]=None
):
    """
        sample_size: number of sentences to generate from sampling
        input_ids: input_ids from source
        prev_gen: previously generated sentence class
    """
    generations = []
    completions = []

    stopping_criteria = StoppingCriteriaList()

    assert not (prev_gen is not None and decoder_input_ids is not None)

    if decoder_input_ids is not None: # batch generations
        decoder_input_id_length = decoder_input_ids.size(-1)
        init_beam_scores = decoder_logsums.unsqueeze(-1).expand(-1, sample_size).reshape(-1)
    else: # either first generation or subsequent single generationsf
        decoder_input_ids = prev_gen.token_ids if prev_gen is not None else None
        decoder_input_id_length = prev_gen.token_ids.size(-1) if prev_gen is not None else 1
        init_beam_scores = (torch.ones(input_ids.size(0)) * prev_gen.logsum).to(input_ids.device) if prev_gen is not None else None

    stop_by_tokens += decoder_input_id_length
    stopping_criteria.append(EndSpanCriteria(stop_by_tokens=stop_by_tokens))

    beamsearch_outputs = generate_sent( 
        # note we generate and obtain the same num_beams of sequences in beam search, because as num_beams grows the performance worsens
        input_ids, 
        stopping_criteria,
        do_sample=False,
        # top_p = 0,
        num_beams=sample_size,
        decoder_input_ids=decoder_input_ids,
        num_return_sequences=sample_size,
        max_length=max_length,
        init_beam_scores=init_beam_scores,
    )
    
    generations, completions = process_beam_search_span_generation(beamsearch_outputs, decoder_input_id_length)

    return (generations, completions)

# --------- Generation ---------
if args.write:
    fw = open(result_file_path, write_mode, encoding="utf-8")
    # score_fw = open(rouge_file_path, write_mode, encoding="utf-8")
    # gold_file_path = result_file_path.replace(".txt", ".gold")
    # gold_fw = open(gold_file_path, write_mode, encoding="utf-8")
    # logprob_file_path = result_file_path.replace(".txt", ".logprob")
    # logprob_fw = open(logprob_file_path, write_mode, encoding="utf-8")

metric = load_metric("rouge")
total_rouge = {}
avg_rouge = {}
total_gen = 0



for idx in tqdm(range(test_start_idx, total_test_examples)):
# for idx in [2]: # DEBUG  
    total_gen += 1 
    if text_list is not None:
        text = text_list[idx]
        gold = target_list[idx]
    else:
        text = raw_datasets[idx]['article']
        gold = raw_datasets[idx]['highlights']


    input_ids = tokenizer(text,max_length=args.max_source_length,padding=False,truncation=True,return_tensors="pt").input_ids.to(device)
    
    output = None

    if gen_mode == "beam_search_sent": # NOTE: only work for this
        gen_history = []
        target_labels = text.split(" ==> ")[0].split(" | ")
        target_labels = [x.strip() for x in target_labels]

        if args.debug:
            print("target label list:", target_labels)

        beamsearch_stopped = False # used to track if no need for beamsearch

        # for sent_idx, target_label in enumerate(target_labels):
        sent_idx = 0
        completions = [] # completed generations given that beamsearch has stopped

        while True:
            #  pass the full list of label ids 
            #  label for first sentence must be target_label
            #  for subsequent sentences, allow the generated label to be either target_label or next_target_label
            target_ids = [labels2idx[target_label] for target_label in target_labels]

            if sent_idx == 0:
                decoder_input_ids = None
                
                sent_options, beamsearch_stopped = generate_sentence_options(GEN_SIZE, input_ids, target_ids, prev_beamsearch_stopped=beamsearch_stopped, decoder_input_ids = decoder_input_ids)
                if beamsearch_stopped:
                    completions.extend(sent_options)
                    break
                else:
                    gen_history = sort_filter_gen_history(sent_options, BEAM_SIZE) # get top k hypothesis

            else:
                sent_options = []
                for i, prev_item in enumerate(gen_history):
                    if args.debug:
                        prev_label = target_labels[prev_item.curr_label_idx]
                        print("\nprev state: logsum {} | num tokens {} | avg log {} | class prob {} | prev label {} | {}".format(prev_item.logsum,prev_item.num_tokens_generated, prev_item.get_avg_log(), prev_item.classification_score, prev_label, prev_item.text))

                    decoder_input_ids = None
                    
                    batch_options, beamsearch_stopped = generate_sentence_options(GEN_SIZE, input_ids, target_ids, prev_gen=prev_item, prev_beamsearch_stopped=beamsearch_stopped, decoder_input_ids = decoder_input_ids)
                    if beamsearch_stopped:
                        completions.extend(batch_options)
                    else:
                        if args.debug:
                            print("\nnew generations:")
                            for i, gen_item in enumerate(batch_options):
                                curr_label = target_labels[gen_item.curr_label_idx]
                                print("logsum {} | num tokens {} | avg log {} | class prob {} | curr label {} | {}".format(gen_item.logsum,gen_item.num_tokens_generated,gen_item.get_avg_log(), gen_item.classification_score, curr_label, gen_item.text))
                        sent_options.extend(batch_options)
                
                if len(sent_options) == 0:
                    break
                gen_history = sort_filter_gen_history(sent_options, BEAM_SIZE)
                # gen_history = sort_filter_gen_history_with_classification_rank(sent_options, BEAM_SIZE)
            
            sent_idx += 1

            if args.debug:
                print("\nnew state:", len(gen_history))
                for i, gen_item in enumerate(gen_history):
                    curr_label = target_labels[gen_item.curr_label_idx]
                    print("logsum {} | num tokens {} | avg log {} | class prob {} | label {} | {}".format(gen_item.logsum,gen_item.num_tokens_generated,gen_item.get_avg_log(), gen_item.classification_score, curr_label, gen_item.text))
                print("\n\n")

        output_text = sort_filter_gen_history(completions, 1)[0].text
        # output_text = sort_filter_gen_history_with_classification_rank(gen_history, 1)[0].text

    elif gen_mode == "greedy_search": # normal greedy search, for baseline
        outputs = model.generate(
                input_ids=input_ids, 
                max_length=MAX_TARGET_LENGTH, 
                num_beams=1,
        )
        output_text = tokenizer.decode(outputs[0, :], skip_special_tokens=True)
    elif gen_mode == "beam_search": # normal beam search, for baseline
        outputs = model.generate(
                input_ids=input_ids, 
                max_length=MAX_TARGET_LENGTH, 
                num_beams=BS_NUM_BEAMS,
                # gen_mode="beam_search_span", # TODO: for debug purpose only
                # output_scores=True,
                # return_dict_in_generate=True, 
        )
        output_text = tokenizer.decode(outputs[0,:], skip_special_tokens=True)
        # output_text = tokenizer.decode(outputs.sequences[0,:], skip_special_tokens=True)
        # logsum = outputs.prev_beam_scores[0]
        # seq_score = outputs.sequences_scores[0]
    elif gen_mode == "beam_sample":
        gen_history = []
        while len(gen_history) == 0:
            gen_history = [] # clean and redo generation
            target_labels = text.split(" ==> ")[0].split(" | ")
            target_labels = [x.strip() for x in target_labels]

            if args.debug or args.eval_rouge:
                print("target label list:", target_labels)
            for sent_idx, target_label in enumerate(target_labels):
                target_id = labels2idx[target_label]
                if args.debug:
                    print("\n\nsent no:", sent_idx, target_label)
                if sent_idx == 0:
                    sent_options = generate_beamsample_options(GEN_SIZE, input_ids, target_id)
                    gen_history = sent_options
                else:
                    sent_options = []
                    for i, prev_item in enumerate(gen_history):
                        if args.debug:
                            print("\nprev state: logsum {} | num tokens {} | avg log {} | class prob {} | rank {} | {}".format(prev_item.logsum,prev_item.num_tokens_generated, prev_item.get_avg_log(), prev_item.classification_score, prev_item.classification_rank, prev_item.text))
                        batch_options = generate_beamsample_options(GEN_SIZE, input_ids, target_id, prev_gen=prev_item)
                        if args.debug:
                            print("\nnew generations:")
                            for i, gen_item in enumerate(batch_options):
                                print("logsum {} | num tokens {} | avg log {} | class prob {} | rank {} | {}".format(gen_item.logsum,gen_item.num_tokens_generated,gen_item.get_avg_log(), gen_item.classification_score, gen_item.classification_rank, gen_item.text))
                        sent_options.extend(batch_options)
                    gen_history = sort_filter_gen_history(sent_options, BEAM_SIZE)

                if args.debug:
                    print("\nnew state:", len(gen_history))
                    for i, gen_item in enumerate(gen_history):
                        print("logsum {} | num tokens {} | avg log {} | class prob {} | rank {} | {}".format(gen_item.logsum,gen_item.num_tokens_generated,gen_item.get_avg_log(), gen_item.classification_score, gen_item.classification_rank, gen_item.text))
                    print("\n\n")
        output_text = sort_filter_gen_history(gen_history, 1)[0].text
    elif gen_mode == "sample":
        gen_history = [] # clean and redo generation
        target_labels = text.split(" ==> ")[0].split(" | ")
        target_labels = [x.strip() for x in target_labels]

        if args.debug or args.eval_rouge:
            print("target label list:", target_labels)
        for sent_idx, target_label in enumerate(target_labels):
            target_id = labels2idx[target_label]
            if args.debug:
                print("\n\nsent no:", sent_idx, target_label)
            if sent_idx == 0:
                sent_options = generte_sample_options(GEN_SIZE, input_ids, target_id)
                gen_history = sent_options
            else:
                sent_options = []
                for i, prev_item in enumerate(gen_history):
                    if args.debug:
                        print("\nprev state: logsum {} | num tokens {} | avg log {} | class prob {} | rank {} | {}".format(prev_item.logsum,prev_item.num_tokens_generated, prev_item.get_avg_log(), prev_item.classification_score, prev_item.classification_rank, prev_item.text))
                    batch_options = generte_sample_options(GEN_SIZE, input_ids, target_id, prev_gen=prev_item)
                    if args.debug:
                        print("\nnew generations:")
                        for i, gen_item in enumerate(batch_options):
                            print("logsum {} | num tokens {} | avg log {} | class prob {} | rank {} | {}".format(gen_item.logsum,gen_item.num_tokens_generated,gen_item.get_avg_log(), gen_item.classification_score, gen_item.classification_rank, gen_item.text))
                    sent_options.extend(batch_options)
                gen_history = sort_filter_gen_history(sent_options, BEAM_SIZE)

            if args.debug:
                print("\nnew state:", len(gen_history))
                for i, gen_item in enumerate(gen_history):
                    print("logsum {} | num tokens {} | avg log {} | class prob {} | rank {} | {}".format(gen_item.logsum,gen_item.num_tokens_generated,gen_item.get_avg_log(), gen_item.classification_score, gen_item.classification_rank, gen_item.text))
                print("\n\n")
        output_text = sort_filter_gen_history(gen_history, 1)[0].text
    elif gen_mode == "beam_search_span":
        beamsearch_stopped = False # used to track if no need for beamsearch
        finished_generations = []
        gen_history = []
        stop_flag = False

        while stop_flag is False and len(finished_generations) <= 8: # TODO: fix this
        # while stop_flag is False:
            if len(gen_history) == 0:
                sent_options, completed_options = generate_sentence_for_beam_search_span(BS_NUM_BEAMS, input_ids, args.stop_by_tokens, max_length=MAX_TARGET_LENGTH)
            else:
                sent_options = []
                completed_options = []

                ##  prepare tensor for one-batch generation
                seqlen = gen_history[0].token_ids.size(-1) # ensure all components are off the same length
                assert all([prev_item.token_ids.size(-1)==seqlen for prev_item in gen_history])
                decoder_input_ids = torch.ones((len(gen_history), seqlen), device=device, dtype=torch.long) # size (batch_size, seq_len)
                decoder_logsums = torch.zeros(len(gen_history), device=device, dtype=torch.float32)
                
                for i, prev_item in enumerate(gen_history):
                    token_ids = prev_item.token_ids
                    decoder_input_ids[i, :] = token_ids
                    decoder_logsums[i] = prev_item.logsum

                for batch_idx in range(decoder_input_ids.size(0) // args.max_batch_size + 1):
                    start = batch_idx * args.max_batch_size
                    if start >= decoder_input_ids.size(0):
                        break
                    batch_decoder_input_ids = decoder_input_ids[start : start + args.max_batch_size]
                    batch_decoder_logsums = decoder_logsums[start : start + args.max_batch_size]
                    batch_input_ids = input_ids.expand(batch_decoder_input_ids.size(0), -1)
                    batch_options, batch_completions = generate_sentence_for_beam_search_span(BS_NUM_BEAMS, batch_input_ids, args.stop_by_tokens, max_length=MAX_TARGET_LENGTH, decoder_input_ids=batch_decoder_input_ids, decoder_logsums=batch_decoder_logsums)
                    sent_options.extend(batch_options)
                    completed_options.extend(batch_completions) 

            if len(sent_options) == 0:
                stop_flag = True
            else:
                gen_history = sort_filter_gen_history_with_length_penalty(sent_options, BEAM_SIZE)
            finished_generations.extend(completed_options)
            if args.debug:
                print("\nnew state:", len(gen_history))
                for i, gen_item in enumerate(gen_history):
                    print("logsum {} | seq_score {} | {}".format(gen_item.logsum, gen_item.seq_score, gen_item.text))
                print("\n\n")
        if args.debug:
            print("selecting the final output from below:")
            print(len(finished_generations))
            for item in finished_generations:
                print(item.text)
                print(item.get_avg_log())

        if len(finished_generations) == 0:
            finished_generations = gen_history

        output = sort_filter_gen_history_with_length_penalty(finished_generations, 1)[0]
        output_text = tokenizer.decode(output.token_ids, skip_special_tokens=True)
        logsum = output.logsum 
        # seq_score = outputs.seq_score

    if args.write:
        fw.write(output_text+'\n')
        # gold_fw.write(gold+'\n')
        # logprob_fw.write(str(logsum)+' '+str(seq_score)+'\n')
    
    if args.debug:
        print(output_text.encode('utf-8'))

    if args.eval_rouge:
        # get current rouge
        result = metric.compute(predictions=[output_text], references=[gold], use_stemmer=True)
        result = {key: value.mid.fmeasure * 100 for key, value in result.items()}

        # get average rouge
        for key in result.keys():
            if key in avg_rouge:
                total_rouge[key] += result[key]
            else:
                total_rouge[key] = result[key]
        avg_rouge = {key: value/total_gen for key,value in total_rouge.items()}

        if args.debug:
            print(result)
            print("avg rouge:", avg_rouge)
    