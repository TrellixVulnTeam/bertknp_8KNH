# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HugginFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Run BERT on Parsing."""
from __future__ import absolute_import, division, print_function

import argparse
import io
import logging
import os
import random
import sys
from collections import defaultdict, namedtuple

import numpy as np
import torch
from pyknp import KNP, TList
from pytorch_pretrained_bert.file_utils import PYTORCH_PRETRAINED_BERT_CACHE
from pytorch_pretrained_bert.modeling import BertForParsing
from pytorch_pretrained_bert.optimization import BertAdam
from pytorch_pretrained_bert.tokenization import BertTokenizer
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import TensorDataset, DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm, trange

from convert_examples_to_features_utils import get_tokenized_tokens
from input_features import InputFeatures

logger = logging.getLogger(__name__)

# for POS list
POS = {}
REV_POS = {}


class ParsingExample(object):
    """A single training/test example for parsing."""

    def __init__(self,
                 example_id,
                 words,
                 lines,
                 heads=None,
                 token_tags=None,
                 gold_words=None,
                 comment=None,
                 h2z=False):
        self.example_id = example_id
        self.words = words
        self.lines = lines
        self.heads = heads
        self.token_tags = token_tags
        self.token_tag_indices = defaultdict(list)
        self.gold_words = gold_words
        self.comment = comment
        self.h2z = h2z

        if self.h2z is True:
            from copy import deepcopy
            import zenhan
            self.words_orig = deepcopy(self.words)
            self.words = [zenhan.h2z(word) for word in words]

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        s = ""
        # TODO: Resolve import error
        # s += "id: %s" % (printable_text(self.example_id))
        # s += ", word: %s" % (printable_text(" ".join(self.words)))
        # s += ", head: %s" % (printable_text(" ".join(self.heads)))
        return s


class TokenLabelVocabulary(object):
    def __init__(self, namespace, train_examples, num_label=None):
        self.namespace = namespace
        self.num_label = num_label
        self.label_to_index = {}
        self.index_to_label = []

        self.read_examples(train_examples)
        self.add_indices(train_examples)

    def read_examples(self, train_examples):
        for train_example in train_examples:
            for tag in train_example.token_tags[self.namespace]:
                if tag == -1:
                    continue
                if tag not in self.label_to_index:
                    self.label_to_index[tag] = len(self.index_to_label)
                    self.index_to_label.append(tag)

        if self.num_label is None:
            self.num_label = len(self.index_to_label)
        else:
            assert (self.num_label == len(self.index_to_label))

    def add_indices(self, examples):
        for example in examples:
            for tag in example.token_tags[self.namespace]:
                if tag == -1:
                    example.token_tag_indices[self.namespace].append(-1)
                else:
                    example.token_tag_indices[self.namespace].append(self.label_to_index[tag])


def get_head_ids_types(example, feature, result, max_seq_length):
    head_ids = {}
    dpnd_types = {}
    for line_num, line in enumerate(example.lines):
        items = line.split("\t")
        # 1 for [CLS]
        pred_head_id = result.heads[feature.orig_to_tok_index[line_num] + 1]
        # ROOT
        if pred_head_id == max_seq_length - 1:
            pred_head_id = -1
        else:
            pred_head_id = feature.tok_to_orig_index[pred_head_id - 1]
        head_ids[line_num] = pred_head_id
        dpnd_types[line_num] = items[7]
    return head_ids, dpnd_types


def get_sentence_str(example):
    sentence_str = ''
    for line_num, line in enumerate(example.lines):
        items = line.split("\t")
        sentence_str += items[1]
    return sentence_str


def modify_knp_for_tag_or_bunsetsu(tags, head_ids, dpnd_types, mode):
    mrph_id2tag = {}
    for tag in tags:
        for mrph in tag.mrph_list():
            mrph_id2tag[mrph.mrph_id] = tag

    for tag in tags:
        # この基本句内の形態素IDリスト
        in_tag_mrph_ids = {}
        last_mrph_id_in_tag = -1
        for mrph in tag.mrph_list():
            in_tag_mrph_ids[mrph.mrph_id] = 1
            if last_mrph_id_in_tag < mrph.mrph_id:
                last_mrph_id_in_tag = mrph.mrph_id

        for mrph_id in list(in_tag_mrph_ids.keys()):
            # 形態素係り先ID
            mrph_head_id = head_ids[mrph_id]
            # 形態素係り先がROOTの場合は何もしない
            if mrph_head_id == -1:
                break
            # 形態素係り先が基本句外に係る場合: 既存の係り先と異なるかチェック
            if mrph_head_id > last_mrph_id_in_tag:
                new_parent_tag = mrph_id2tag[mrph_head_id]
                if mode == 'tag':
                    new_parent_id = new_parent_tag.tag_id
                    old_parent_id = tag.parent.tag_id
                else:
                    new_parent_id = new_parent_tag.bnst_id
                    old_parent_id = tag.parent.bnst_id
                # 係りタイプの更新
                if dpnd_types[mrph_id] != tag.dpndtype:
                    tag.dpndtype = dpnd_types[mrph_id]
                # 係り先の更新
                if new_parent_id != old_parent_id:
                    # 形態素係り先IDを基本句IDに変換しparentを設定
                    tag.parent_id = new_parent_id
                    tag.parent = new_parent_tag
                    # children要更新?
                    break


def modify_knp(knp_result, head_ids, dpnd_types):
    tags = knp_result.tag_list()
    bnsts = knp_result.bnst_list()

    # modify tag dependencies
    modify_knp_for_tag_or_bunsetsu(tags, head_ids, dpnd_types, 'tag')

    # modify bnst dependencies
    modify_knp_for_tag_or_bunsetsu(bnsts, head_ids, dpnd_types, 'bunsetsu')


def sprint_tag_tree(knp_result):
    tlist = TList()
    for tag in knp_result.tag_list():
        tlist.push_tag(tag)
    return tlist.sprint_tree()


def write_knp_result_from_conllu(knp_dpnd, knp_case, all_examples, all_features, all_results, writer, max_seq_length,
                                 output_tree=False):
    # convert a result to KNP format
    for examples, features, results in zip(all_examples, all_features, all_results):
        knp_result = knp_dpnd.parse(get_sentence_str(examples))
        knp_result.comment = examples.comment
        head_ids, dpnd_types = get_head_ids_types(examples, features, results, max_seq_length)
        modify_knp(knp_result, head_ids, dpnd_types)

        # add predicate-argument structures by KNP
        knp_result_new = knp_case.reparse_knp_result(knp_result.all().strip())
        if output_tree:
            writer.write(sprint_tag_tree(knp_result_new))
        else:
            writer.write(knp_result_new.all())


def read_pos_list(pos_list_file):
    with open(pos_list_file, encoding='utf-8') as poslist:
        for line in poslist:
            items = line.strip().split('\t')
            POS[items[0]] = items[1]
            REV_POS[items[1]] = items[0]


def get_pos(pos, spos):
    if spos == '*':
        key = pos
    elif pos == '未定義語':
        key = '未定義語-その他'
    else:
        key = '%s-%s' % (pos, spos)

    if key in POS:
        return POS[key]
    else:
        assert 'Unknown POS'


def jpp2conll_one_sentence(buf):
    output_lines = []
    prev_id = 0
    for line in buf.splitlines():
        # comment line
        if line.startswith("#"):
            output_lines.append(line + '\n')
            continue
        result = []
        if line.startswith('EOS'):
            break
        items = line.strip().split('\t')

        if prev_id == items[1]:
            continue  # skip the same id
        else:
            result.append(str(items[1]))
            prev_id = items[1]
        result.append(items[5])  # midasi
        result.append(items[8])  # genkei
        conll_pos = get_pos(items[9], items[11])  # hinsi, bunrui
        result.append(conll_pos)
        result.append(conll_pos)
        result.append('_')
        if len(items) > 19:
            result.append(items[18])  # head
            result.append(items[19])  # dpnd_type
        else:
            result.append('0')  # head
            result.append('D')  # dpnd_type (dummy)
        result.append('_')
        result.append('_')
        output_lines.append('\t'.join(result) + '\n')
    return ''.join(output_lines) + '\n'


def read_parsing_examples(input_file, is_training,
                          parsing=False,
                          word_segmentation=False, pos_tagging=False, subpos_tagging=False, feats_tagging=False,
                          estimate_dep_label=False, use_gold_segmentation_in_test=False, use_gold_pos_in_test=False,
                          h2z=False, knp_mode=False, multi_sentences=True):
    """Read a file into a list of ParsingExample."""
    buf_conll = ''
    if knp_mode is True:
        # convert Juman++ (-s 1) to CoNLL
        buf = ''
        for line in sys.stdin:
            buf += line
            if line.strip() == 'EOS':
                buf_conll += jpp2conll_one_sentence(buf)
                buf = ''
                if multi_sentences is False:
                    break
    else:
        with open(input_file, encoding='utf-8') as f:
            for line in f:
                buf_conll += line

    if not buf_conll:
        return []

    return read_parsing_examples_from_buf(buf_conll, is_training, parsing, word_segmentation, pos_tagging,
                                          subpos_tagging, feats_tagging, estimate_dep_label,
                                          use_gold_segmentation_in_test, use_gold_pos_in_test, h2z)


def read_parsing_examples_from_buf(buf, is_training,
                                   parsing=False,
                                   word_segmentation=False, pos_tagging=False, subpos_tagging=False,
                                   feats_tagging=False,
                                   estimate_dep_label=False, use_gold_segmentation_in_test=False,
                                   use_gold_pos_in_test=False, h2z=False):
    """Read a buffer into a list of ParsingExample."""

    examples = []
    example_id = 0

    # 1       村山    村山    NNP     NNP     _       2       D       _       _
    words, heads, lines, word_to_char_index = [], [], [], []
    token_tags = defaultdict(list)
    gold_words = []
    comment = None
    for line in buf.splitlines():
        line = line.strip()
        if line.startswith("#") is True:
            comment = line
            continue
        if not line:
            if word_segmentation is True or use_gold_segmentation_in_test is True:
                if is_training is True:
                    # convert word to char indices (except -1 and 0 (Root))
                    heads = [word_to_char_index[head - 1] + 1 if head != -1 and head != 0 else head for head in heads]
                else:
                    heads = [-1] * len(heads)
            example = ParsingExample(
                example_id,
                words,
                lines,
                heads=heads,
                token_tags=token_tags,
                comment=comment,
                gold_words=gold_words if use_gold_segmentation_in_test else None,
                h2z=h2z)
            examples.append(example)

            example_id += 1
            words, heads, lines, word_to_char_index = [], [], [], []
            token_tags = defaultdict(list)
            gold_words = []
            comment = None
            continue

        items = line.split("\t")
        word = items[1]
        head = int(items[6])
        if word_segmentation is True or use_gold_segmentation_in_test is True:
            if is_training is False:
                head = -1
                if use_gold_pos_in_test is False:
                    items[3] = -1
                    items[4] = -1
                if estimate_dep_label is True:
                    items[7] = "dummy"
            chars, _token_tags, _heads = get_outputs_for_word_segmentation(word, head, items, word_to_char_index,
                                                                           is_training=is_training,
                                                                           word_segmentation=word_segmentation,
                                                                           parsing=parsing,
                                                                           char_offset=len(words),
                                                                           pos_tagging=pos_tagging,
                                                                           subpos_tagging=subpos_tagging,
                                                                           feats_tagging=feats_tagging,
                                                                           estimate_dep_label=estimate_dep_label)
            words.extend(chars)
            for namespace in _token_tags:
                token_tags[namespace].extend(_token_tags[namespace])
            heads.extend(_heads)
            if use_gold_segmentation_in_test is True:
                gold_words.append(word)
            if use_gold_pos_in_test is True:
                lines.append(line)
        else:
            if is_training is False:
                head = -1
                items[6] = "-1"
                line = "\t".join(items)

            words.append(word)
            heads.append(head)
            lines.append(line)

    return examples


def get_outputs_for_word_segmentation(word, head, items, word_to_char_index,
                                      is_training=True,
                                      word_segmentation=False,
                                      parsing=False,
                                      char_offset=None,
                                      pos_tagging=False,
                                      subpos_tagging=False,
                                      feats_tagging=False,
                                      estimate_dep_label=False):
    chars = list(word)
    char_num = len(chars)

    _heads = []
    _token_tags = defaultdict(list)
    for i, char in enumerate(list(word)):
        if word_segmentation is True:
            word_segmentation_tag = get_word_segmentation_tag(i, char_num, is_training)
            _token_tags["word_segmentation"].append(word_segmentation_tag)
        if i == 0:
            if parsing is True:
                _heads.append(int(head))
            else:
                _heads.append(-1)
            if pos_tagging is True:
                _token_tags["pos"].append(items[3])
            if subpos_tagging is True:
                _token_tags["subpos"].append(items[4])
            if feats_tagging is True:
                _token_tags["feats"].append(items[5])
            if estimate_dep_label is True:
                _token_tags["dep_label"].append(items[7])
            word_to_char_index.append(i + char_offset)
        else:
            _heads.append(-1)
            if pos_tagging is True:
                _token_tags["pos"].append(-1)
            if subpos_tagging is True:
                _token_tags["subpos"].append(-1)
            if feats_tagging is True:
                _token_tags["feats"].append(-1)
            if estimate_dep_label is True:
                _token_tags["dep_label"].append(-1)

    return chars, _token_tags, _heads


def get_word_segmentation_tag(i, char_num, is_training):
    """ BIE tagging """

    if is_training is False:
        return -1

    if i == 0:
        return "B"
    elif i == char_num - 1:
        return "E"
    else:
        return "I"


def convert_examples_to_features(examples, tokenizer, max_seq_length, vocab_size,
                                 is_training, word_segmentation=False,
                                 use_gold_segmentation_in_test=False,
                                 num_special_tokens=1, special_tokens=None):
    """Loads a data file into a list of `InputBatch`s."""

    unique_id = 1000000000

    features = []

    for (example_index, example) in enumerate(examples):
        # The -3 accounts for [CLS], [SEP], ROOT
        # max_tokens_for_doc = max_seq_length - 3

        tokens = []
        segment_ids = []
        heads = []
        token_tag_indices = defaultdict(list)

        all_tokens, tok_to_orig_index, orig_to_tok_index = get_tokenized_tokens(example.words, tokenizer)

        # CLS
        tokens.append("[CLS]")
        segment_ids.append(0)
        heads.append(-1)
        if word_segmentation is True or use_gold_segmentation_in_test is True:
            for namespace in example.token_tag_indices:
                token_tag_indices[namespace].append(-1)

        for j, token in enumerate(all_tokens):
            tokens.append(token)
            if word_segmentation is True or use_gold_segmentation_in_test is True:
                for namespace in example.token_tag_indices:
                    token_tag_indices[namespace].append(example.token_tag_indices[namespace][tok_to_orig_index[j]])

            # parsing
            if is_training is False:
                head_id = -1
            else:
                head = example.heads[tok_to_orig_index[j]]
                # ROOT
                if head == 0:
                    head_id = max_seq_length - 1
                elif token.startswith("##") is True or head == -1:
                    head_id = -1
                else:
                    head_id = orig_to_tok_index[head - 1] + 1
            heads.append(head_id)

            segment_ids.append(0)

        # SEP
        tokens.append("[SEP]")
        heads.append(-1)
        if word_segmentation is True or use_gold_segmentation_in_test is True:
            for namespace in example.token_tag_indices:
                token_tag_indices[namespace].append(-1)
        segment_ids.append(0)

        input_ids = tokenizer.convert_tokens_to_ids(tokens)

        # The mask has 1 for real tokens and 0 for padding tokens. Only real
        # tokens are attended to.
        input_mask = [1] * len(input_ids)

        # Zero-pad up to the sequence length (except for ROOT).
        while len(input_ids) < max_seq_length - num_special_tokens:
            input_ids.append(0)
            input_mask.append(0)
            heads.append(-1)
            if word_segmentation is True or use_gold_segmentation_in_test is True:
                for namespace in example.token_tag_indices:
                    token_tag_indices[namespace].append(-1)
            segment_ids.append(0)

        # ROOT
        input_ids.append(vocab_size)
        input_mask.append(1)
        heads.append(-1)
        segment_ids.append(0)
        if word_segmentation is True or use_gold_segmentation_in_test is True:
            for namespace in example.token_tag_indices:
                token_tag_indices[namespace].append(-1)

        assert len(input_ids) == max_seq_length, "input_ids_length ({}) is greater than max_seq_length ({})".format(
            len(input_ids), max_seq_length)
        assert len(input_mask) == max_seq_length
        assert len(segment_ids) == max_seq_length
        for namespace in token_tag_indices:
            assert len(token_tag_indices[namespace]) == max_seq_length

        assert len(heads) == max_seq_length

        if example_index < 20:
            logger.info("*** Example ***")
            logger.info("unique_id: %s" % unique_id)
            logger.info("example_index: %s" % example_index)
            logger.info("tokens: %s" % " ".join(
                [x for x in tokens]))
            logger.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
            logger.info(
                "input_mask: %s" % " ".join([str(x) for x in input_mask]))
            logger.info(
                "segment_ids: %s" % " ".join([str(x) for x in segment_ids]))
            logger.info(
                "heads: %s" % " ".join([str(x) for x in heads]))
            for namespace in token_tag_indices:
                logger.info(
                    "%s_tags: %s" % (namespace, " ".join([str(x) for x in token_tag_indices[namespace]])))

        features.append(
            InputFeatures(
                unique_id=unique_id,
                example_index=example_index,
                tokens=tokens,
                orig_to_tok_index=orig_to_tok_index,
                tok_to_orig_index=tok_to_orig_index,
                input_ids=input_ids,
                input_mask=input_mask,
                segment_ids=segment_ids,
                heads=heads,
                token_tag_indices=token_tag_indices))
        unique_id += 1

    return features


RawResult = namedtuple("RawResult",
                       ["unique_id", "heads", "topk_heads", "topk_dep_labels", "token_tags",
                        "top_spans", "antecedent_indices", "predicted_antecedents",
                        "antecedent_labels_set"])


def write_predictions(all_examples, all_features, all_results, output_prediction_file, max_seq_length,
                      knp_dpnd, knp_case, num_special_tokens=None, special_tokens=None,
                      parsing=False,
                      word_segmentation=False, pos_tagging=False, subpos_tagging=False, feats_tagging=False,
                      estimate_dep_label=False, token_label_vocabulary=None,
                      use_gold_segmentation_in_test=False, use_gold_pos_in_test=False,
                      chinese_zero=False, knp_mode=False, output_tree=False):
    """Write final predictions to the file."""
    if output_prediction_file is not None:
        logger.info("Writing predictions to: %s" % output_prediction_file)

    if knp_mode:
        writer = sys.stdout
        write_knp_result_from_conllu(knp_dpnd, knp_case, all_examples, all_features, all_results, writer,
                                     max_seq_length, output_tree)
        writer.flush()
    else:
        writer = open(output_prediction_file, "w", encoding="utf-8")
        for (example_index, example) in enumerate(all_examples):
            feature = all_features[example_index]
            result = all_results[example_index]
            if example.comment is not None:
                writer.write("{}\n".format(example.comment))

            if word_segmentation is True or use_gold_segmentation_in_test is True or use_gold_pos_in_test is True:
                write_predictions_word_segmentation(example, result, feature, example_index, writer, max_seq_length,
                                                    token_label_vocabulary=token_label_vocabulary,
                                                    parsing=parsing,
                                                    pos_tagging=pos_tagging, subpos_tagging=subpos_tagging,
                                                    feats_tagging=feats_tagging,
                                                    estimate_dep_label=estimate_dep_label,
                                                    use_gold_segmentation_in_test=use_gold_segmentation_in_test,
                                                    use_gold_pos_in_test=use_gold_pos_in_test)
            else:
                zp_index = 0
                for line_num, line in enumerate(example.lines):
                    items = line.split("\t")
                    if chinese_zero is True:
                        items = output_chinese_zero(items, result, example, zp_index)
                        if items[2] != "_":
                            zp_index += 1
                    else:
                        # 1 for [CLS]
                        pred_head_id = result.heads[all_features[example_index].orig_to_tok_index[line_num] + 1]
                        # ROOT
                        if pred_head_id == max_seq_length - 1:
                            pred_head_id = 0
                        else:
                            pred_head_id = feature.tok_to_orig_index[pred_head_id - 1] + 1

                        items[6] = str(pred_head_id)
                    writer.write("{}\n".format("\t".join(items)))

            writer.write("\n")
        writer.close()


class Word(object):
    def __init__(self, char_index):
        self.char_index = char_index
        self.string = ""
        self.char_indices = []
        self.parent_word_index = None


def write_predictions_word_segmentation(example, result, feature, example_index, writer, max_seq_length,
                                        token_label_vocabulary=None, parsing=False, pos_tagging=False,
                                        subpos_tagging=False, feats_tagging=False,
                                        estimate_dep_label=False, use_gold_segmentation_in_test=False,
                                        use_gold_pos_in_test=False):
    words, char_to_word_index = [], []
    if use_gold_segmentation_in_test is True:
        char_offset = 0
        for word in example.gold_words:
            words.append(Word(char_offset))
            words[-1].string = word
            for i, char in enumerate(list(word)):
                char_to_word_index.append(len(words) - 1)
                words[-1].char_indices.append(char_offset + i)
            char_offset += len(word)
    else:
        # this "words" means characters
        for i, char in enumerate(example.words_orig if example.h2z is True else example.words):
            # B tag
            if i == 0 or token_label_vocabulary["word_segmentation"].index_to_label[
                result.token_tags["word_segmentation"][feature.orig_to_tok_index[i] + 1]] == "B":
                words.append(Word(i))
            words[-1].string += char
            words[-1].char_indices.append(i)
            char_to_word_index.append(len(words) - 1)

    root_exist = False
    for i, word in enumerate(words):
        head_id = None
        dep_label = None
        if parsing is True:
            for k, head_char_id in enumerate(result.topk_heads[feature.orig_to_tok_index[word.char_index] + 1]):
                # ROOT
                if head_char_id == max_seq_length - 1:
                    if root_exist is True:
                        continue
                    head_id = 0
                    if estimate_dep_label is True:
                        dep_label = result.topk_dep_labels[feature.orig_to_tok_index[word.char_index] + 1][k]
                    root_exist = True
                    break
                else:
                    # target word itself
                    if head_char_id - 1 in word.char_indices:
                        continue
                    # out of index
                    elif head_char_id - 1 >= len(char_to_word_index):
                        continue
                    # not to make cycle
                    elif has_cycle(head_char_id, char_to_word_index, words, i) is True:
                        continue
                    else:
                        word.parent_word_index = char_to_word_index[head_char_id - 1]
                        head_id = char_to_word_index[head_char_id - 1] + 1
                        if estimate_dep_label is True:
                            dep_label = result.topk_dep_labels[feature.orig_to_tok_index[word.char_index] + 1][k]
                        break
            assert head_id is not None
        else:
            # dummy head (the following is for assuming there are one root node in a sentence)
            if i == 0:
                head_id = 0
            else:
                head_id = 1
            dep_label = "dummy"

        if use_gold_pos_in_test is True:
            items = example.lines[i].split("\t")
        if pos_tagging is True:
            pos = token_label_vocabulary["pos"].index_to_label[
                result.token_tags["pos"][feature.orig_to_tok_index[word.char_index] + 1]]
        else:
            if use_gold_pos_in_test is True:
                pos = items[3]
            else:
                pos = "dummy"
        if subpos_tagging is True:
            subpos = token_label_vocabulary["subpos"].index_to_label[
                result.token_tags["subpos"][feature.orig_to_tok_index[word.char_index] + 1]]
        else:
            if use_gold_pos_in_test is True:
                subpos = items[4]
            else:
                subpos = "_"
        if feats_tagging is True:
            feats = token_label_vocabulary["feats"].index_to_label[
                result.token_tags["feats"][feature.orig_to_tok_index[word.char_index] + 1]]
        else:
            if use_gold_pos_in_test is True:
                feats = items[5]
            else:
                feats = "_"
        if estimate_dep_label is True:
            dep_label = token_label_vocabulary["dep_label"].index_to_label[dep_label]
        else:
            dep_label = "dummy"

        writer.write(
            "{index}\t{word}\t{word}\t{pos}\t{subpos}\t{feats}\t{head}\t{dep_label}\t_\t_\n".format(
                index=i + 1,
                word=word.string,
                pos=pos,
                subpos=subpos,
                feats=feats,
                head=head_id,
                dep_label=dep_label))


def has_cycle(head_char_id, char_to_word_index, words, target_word_index):
    head_word_index = char_to_word_index[head_char_id - 1]
    while True:
        if words[head_word_index].parent_word_index is None:
            return False

        # cycle
        if words[head_word_index].parent_word_index == target_word_index:
            return True

        head_word_index = words[head_word_index].parent_word_index

    return False


def output_chinese_zero(items, result, example, zp_index):
    if items[2] != "_":
        candidate_strings = []
        # 1-15%0,1-4%0,1-2%0, ..
        for candidates_labels, antecedent_label in zip(example.candidates_labels_set[zp_index],
                                                       result.antecedent_labels_set[zp_index]):
            candidate_strings.append(
                "{}-{}%{:.5f}".format(candidates_labels[0], candidates_labels[1], antecedent_label[1]))

        items[2] = ",".join(candidate_strings)

    return items


def copy_optimizer_params_to_model(named_params_model, named_params_optimizer):
    """ Utility function for optimize_on_cpu and 16-bits training.
        Copy the parameters optimized on CPU/RAM back to the model on GPU
    """
    for (name_opti, param_opti), (name_model, param_model) in zip(named_params_optimizer, named_params_model):
        if name_opti != name_model:
            logger.error("name_opti != name_model: {} {}".format(name_opti, name_model))
            raise ValueError
        param_model.data.copy_(param_opti.data)


def set_optimizer_params_grad(named_params_optimizer, named_params_model, test_nan=False):
    """ Utility function for optimize_on_cpu and 16-bits training.
        Copy the gradient of the GPU parameters to the CPU/RAMM copy of the model
    """
    is_nan = False
    for (name_opti, param_opti), (name_model, param_model) in zip(named_params_optimizer, named_params_model):
        if name_opti != name_model:
            logger.error("name_opti != name_model: {} {}".format(name_opti, name_model))
            raise ValueError
        if param_model.grad is not None:
            if test_nan and torch.isnan(param_model.grad).sum() > 0:
                is_nan = True
            if param_opti.grad is None:
                param_opti.grad = torch.nn.Parameter(param_opti.data.new().resize_(*param_opti.data.size()))
            param_opti.grad.data.copy_(param_model.grad.data)
        else:
            param_opti.grad = None
    return is_nan


def main():
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument("--bert_model", default=None, type=str, required=True,
                        help="Bert pre-trained model selected in the list: bert-base-uncased, "
                             "bert-large-uncased, bert-base-cased, bert-base-multilingual, bert-base-chinese.")
    parser.add_argument("--output_dir", default=None, type=str, required=True,
                        help="The output directory where the model checkpoints will be written.")

    # Other parameters
    parser.add_argument("--train_file", default=None, type=str, help="SQuAD json for training. E.g., train-v1.1.json")
    parser.add_argument("--predict_file", default=None, type=str,
                        help="SQuAD json for predictions. E.g., dev-v1.1.json or test-v1.1.json")
    parser.add_argument("--prediction_result_filename", default="predictions.txt", type=str,
                        help="Prediction result filename. If this option is '-', use stdout.")
    parser.add_argument("--max_seq_length", default=384, type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. Sequences "
                             "longer than this will be truncated, and sequences shorter than this will be padded.")
    parser.add_argument("--doc_stride", default=128, type=int,
                        help="When splitting up a long document into chunks, how much stride to take between chunks.")
    parser.add_argument("--max_query_length", default=64, type=int,
                        help="The maximum number of tokens for the question. Questions longer than this will "
                             "be truncated to this length.")
    parser.add_argument("--do_train", default=False, action='store_true', help="Whether to run training.")
    parser.add_argument("--do_predict", default=False, action='store_true', help="Whether to run eval on the dev set.")
    parser.add_argument("--train_batch_size", default=32, type=int, help="Total batch size for training.")
    parser.add_argument("--predict_batch_size", default=8, type=int, help="Total batch size for predictions.")
    parser.add_argument("--learning_rate", default=5e-5, type=float, help="The initial learning rate for Adam.")
    parser.add_argument("--num_train_epochs", default=3.0, type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--warmup_proportion", default=0.1, type=float,
                        help="Proportion of training to perform linear learning rate warmup for. E.g., 0.1 = 10% "
                             "of training.")
    parser.add_argument("--no_cuda",
                        default=False,
                        action='store_true',
                        help="Whether not to use CUDA when available")
    parser.add_argument('--seed',
                        type=int,
                        default=42,
                        help="random seed for initialization")
    parser.add_argument('--gradient_accumulation_steps',
                        type=int,
                        default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument("--do_lower_case",
                        default=False,
                        action='store_true',
                        help="Whether to lower case the input text. True for uncased models, False for cased models.")
    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on gpus")
    parser.add_argument('--optimize_on_cpu',
                        default=False,
                        action='store_true',
                        help="Whether to perform optimization and keep the optimizer averages on CPU")
    parser.add_argument('--fp16',
                        default=False,
                        action='store_true',
                        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument('--loss_scale',
                        type=float, default=128,
                        help='Loss scaling, positive power of 2 values can improve fp16 convergence.')
    parser.add_argument("--special_tokens", default=None, type=str,
                        help="Special tokens.")
    parser.add_argument("--finetuning_added_tokens", default=None, type=str,
                        help="Added tokens for only fine-tuning.")
    parser.add_argument("--parsing", default=False, action='store_true', help="Perform parsing.")
    parser.add_argument("--word_segmentation", default=False, action='store_true', help="Perform word segmentation.")
    parser.add_argument("--use_gold_segmentation_in_test", default=False, action='store_true',
                        help="Use gold segmentations in testing")
    parser.add_argument("--use_gold_pos_in_test", default=False, action='store_true', help="Use gold POSs in testing")
    parser.add_argument("--pos_tagging", default=False, action='store_true', help="Perform POS tagging.")
    parser.add_argument("--subpos_tagging", default=False, action='store_true', help="Perform SubPOS tagging.")
    parser.add_argument("--feats_tagging", default=False, action='store_true', help="Perform Feats tagging.")
    parser.add_argument("--parsing_algorithm", choices=["biaffine", "zhang"], default="zhang",
                        help="biaffine [Dozat+ 17] or zhang [Zhang+ 16]")
    parser.add_argument("--estimate_dep_label", default=False, action='store_true', help="Estimate dependency labels.")
    parser.add_argument("--chinese_zero", default=False, action='store_true',
                        help="Perform zero anaphora resolution (Chinese).")
    parser.add_argument("--num_max_text_length",
                        type=int,
                        default=None,
                        help="Maximum number of text length")
    parser.add_argument("--use_training_data_ratio", default=None, type=float, help="Used training data ratio.")
    parser.add_argument("--lang", default="ja", type=str, help="Language.")
    parser.add_argument("--h2z", default=False, action='store_true', help="Hankaku to Zenkaku.")
    parser.add_argument("--knp_mode", default=False, action='store_true',
                        help="KNP mode (stdin: jumanpp -s 1, stdout: KNP format.")
    parser.add_argument("--output_tree", default=False, action='store_true', help="Output trees.")
    parser.add_argument("--pos_list", default=None, type=str,
                        help="Specify a pos.list file to convert a Juman++ file to CoNLL.")
    parser.add_argument("--single_sentence", default=False, action='store_true',
                        help="If you use bertknp from pyknp, you should specify this flag.")

    args = parser.parse_args()
    args = postprocess_args(args)

    if args.knp_mode:
        # read the pos_list file is specified
        if args.pos_list:
            read_pos_list(args.pos_list)
        knp_dpnd = KNP(option="-tab -disable-segmentation-modification -dpnd-fast")
        knp_case = KNP(option="-tab -disable-segmentation-modification -case2")
    else:
        knp_dpnd = None
        knp_case = None

    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        n_gpu = torch.cuda.device_count()
    else:
        device = torch.device("cuda", args.local_rank)
        n_gpu = 1
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.distributed.init_process_group(backend='nccl')
        if args.fp16:
            logger.info("16-bits training currently not supported in distributed training")
            args.fp16 = False  # (see https://github.com/pytorch/pytorch/pull/13496)
    logger.info("device: {} n_gpu: {}, distributed training: {}, 16-bits trainiing: {}".format(
        device, n_gpu, bool(args.local_rank != -1), args.fp16))

    if args.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
            args.gradient_accumulation_steps))

    args.train_batch_size = int(args.train_batch_size / args.gradient_accumulation_steps)

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if not args.do_train and not args.do_predict:
        raise ValueError("At least one of `do_train` or `do_predict` must be True.")

    if args.do_train:
        if not args.train_file:
            raise ValueError(
                "If `do_train` is True, then `train_file` must be specified.")

    os.makedirs(args.output_dir, exist_ok=True)

    tokenizer = BertTokenizer.from_pretrained(args.bert_model, do_lower_case=False, lang=args.lang)

    num_expand_vocab, finetuning_added_tokens, num_finetuning_added_tokens, special_tokens, num_special_tokens = \
        preprocess_vocab(tokenizer, args)

    output_model_file = os.path.join(args.output_dir, "pytorch_model.bin")
    vocab_size = None
    token_label_vocabulary = {}
    output_token_label_vocabulary = None
    if args.word_segmentation is True or args.use_gold_segmentation_in_test is True:
        output_token_label_vocabulary = os.path.join(args.output_dir, "token_label_vocabulary.bin")

    if args.do_train:
        if args.chinese_zero is True:
            # TODO: Resolve import error
            raise ImportError
            # from chinese_zero import read_chinese_zero_examples, convert_examples_to_features_chinese_zero
            # train_examples = read_chinese_zero_examples(input_file=args.train_file, is_training=True)
        else:
            train_examples = read_parsing_examples(
                input_file=args.train_file, is_training=True,
                parsing=args.parsing,
                word_segmentation=args.word_segmentation, pos_tagging=args.pos_tagging,
                subpos_tagging=args.subpos_tagging, feats_tagging=args.feats_tagging,
                use_gold_segmentation_in_test=args.use_gold_segmentation_in_test,
                estimate_dep_label=args.estimate_dep_label, h2z=args.h2z, knp_mode=args.knp_mode,
                multi_sentences=(not args.single_sentence))
        if args.use_training_data_ratio is not None:
            num_train_example = int(len(train_examples) * args.use_training_data_ratio)
            train_examples = train_examples[:num_train_example]
        num_train_steps = int(
            len(train_examples) / args.train_batch_size / args.gradient_accumulation_steps * args.num_train_epochs)

        if args.word_segmentation is True:
            token_label_vocabulary["word_segmentation"] = TokenLabelVocabulary("word_segmentation", train_examples,
                                                                               num_label=3)
        if args.pos_tagging is True:
            token_label_vocabulary["pos"] = TokenLabelVocabulary("pos", train_examples)
        if args.subpos_tagging is True:
            token_label_vocabulary["subpos"] = TokenLabelVocabulary("subpos", train_examples)
        if args.feats_tagging is True:
            token_label_vocabulary["feats"] = TokenLabelVocabulary("feats", train_examples)
        if args.estimate_dep_label is True:
            token_label_vocabulary["dep_label"] = TokenLabelVocabulary("dep_label", train_examples)

        # Prepare model
        if args.chinese_zero is True:
            # TODO: Resolve import error
            raise ImportError
            # from pytorch_pretrained_bert.modeling_chinese_zero import BertForChineseZero
            # model = BertForChineseZero.from_pretrained(
            #     args.bert_model,
            #     cache_dir=PYTORCH_PRETRAINED_BERT_CACHE / 'distributed_{}'.format(args.local_rank))
        else:
            model = BertForParsing.from_pretrained(args.bert_model,
                                                   cache_dir=PYTORCH_PRETRAINED_BERT_CACHE / 'distributed_{}'.format(
                                                       args.local_rank),
                                                   token_label_vocabulary=token_label_vocabulary,
                                                   parsing_algorithm=args.parsing_algorithm,
                                                   estimate_dep_label=args.estimate_dep_label)

        # Add special embeddings (special tokens, finetuning_added_tokens)
        if num_expand_vocab > 0:
            model.bert.expand_vocab(num_expand_vocab=num_expand_vocab)
            if num_finetuning_added_tokens > 0:
                add_vocab(finetuning_added_tokens, tokenizer, model)

        vocab_size = model.config.vocab_size + num_finetuning_added_tokens

        if args.fp16:
            model.half()
        model.to(device)
        if args.local_rank != -1:
            model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank],
                                                              output_device=args.local_rank)
        elif n_gpu > 1:
            model = torch.nn.DataParallel(model)

        # Prepare optimizer
        if args.fp16:
            param_optimizer = [(n, param.clone().detach().to('cpu').float().requires_grad_()) \
                               for n, param in model.named_parameters()]
        elif args.optimize_on_cpu:
            param_optimizer = [(n, param.clone().detach().to('cpu').requires_grad_()) \
                               for n, param in model.named_parameters()]
        else:
            param_optimizer = list(model.named_parameters())
        no_decay = ['bias', 'gamma', 'beta']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)],
             'weight_decay_rate': 0.01},
            {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay_rate': 0.0}
        ]
        t_total = num_train_steps
        if args.local_rank != -1:
            t_total = t_total // torch.distributed.get_world_size()
        optimizer = BertAdam(optimizer_grouped_parameters,
                             lr=args.learning_rate,
                             warmup=args.warmup_proportion,
                             t_total=t_total)

        global_step = 0
        if args.chinese_zero is True:
            # TODO: Resolve import error
            raise ImportError
            # train_features = convert_examples_to_features_chinese_zero(
            #     examples=train_examples,
            #     tokenizer=tokenizer,
            #     max_seq_length=args.max_seq_length,
            #     is_training=True,
            #     logger=logger)
        else:
            train_features = convert_examples_to_features(
                examples=train_examples,
                tokenizer=tokenizer,
                max_seq_length=args.max_seq_length,
                vocab_size=vocab_size,
                is_training=True,
                word_segmentation=args.word_segmentation,
                use_gold_segmentation_in_test=args.use_gold_segmentation_in_test,
                num_special_tokens=num_special_tokens,
                special_tokens=special_tokens)
        logger.info("***** Running training *****")
        logger.info("  Num orig examples = %d", len(train_examples))
        logger.info("  Batch size = %d", args.train_batch_size)
        logger.info("  Num steps = %d", num_train_steps)
        all_input_ids = torch.tensor([f.input_ids for f in train_features], dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in train_features], dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in train_features], dtype=torch.long)

        # chinese_zero
        if args.chinese_zero is True:
            all_zps = pad_sequence([torch.tensor(f.zps, dtype=torch.long) for f in train_features], batch_first=True,
                                   padding_value=-1)
            all_candidates_labels_set = get_all_pad_candidates_labels_set(train_features)
            train_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_zps,
                                       all_candidates_labels_set)
        # word_segmentation, pos tagging, parsing
        else:
            all_heads = torch.tensor([f.heads for f in train_features], dtype=torch.long)
            if args.word_segmentation is True or args.use_gold_segmentation_in_test is True:
                all_token_tags = []
                for namespace in sorted(token_label_vocabulary.keys()):
                    all_token_tags.append(
                        torch.tensor([f.token_tag_indices[namespace] for f in train_features], dtype=torch.long))
                if args.parsing is True:
                    train_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_heads,
                                               *all_token_tags)
                else:
                    train_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, *all_token_tags)
            else:
                train_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_heads)

        if args.local_rank == -1:
            train_sampler = RandomSampler(train_data)
        else:
            train_sampler = DistributedSampler(train_data)
        train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=args.train_batch_size)

        model.train()
        for i in trange(int(args.num_train_epochs), desc="Epoch"):
            tr_loss = 0
            nb_tr_steps = 0
            for step, batch in enumerate(tqdm(train_dataloader, desc="Iteration")):
                if n_gpu == 1:
                    batch = tuple(t.to(device) for t in batch)  # multi-gpu does scattering it-self

                if args.chinese_zero is True:
                    input_ids, input_mask, segment_ids, zps, candidates_labels_set = batch
                    loss = model(input_ids, segment_ids, input_mask, zps, candidates_labels_set)
                else:
                    token_tags = None
                    if args.word_segmentation is True or args.use_gold_segmentation_in_test is True:
                        token_tags = {}
                        if args.parsing is True:
                            input_ids, input_mask, segment_ids, heads, *token_tags_array = batch
                        else:
                            input_ids, input_mask, segment_ids, *token_tags_array = batch
                            heads = None
                        for namespace, _token_tags in zip(sorted(token_label_vocabulary.keys()), token_tags_array):
                            token_tags[namespace] = _token_tags
                    else:
                        input_ids, input_mask, segment_ids, heads = batch
                    loss = model(input_ids, segment_ids, input_mask, heads=heads, token_tags=token_tags)
                tr_loss, nb_tr_steps, global_step = update_parameters(args, loss, n_gpu, tr_loss, step, nb_tr_steps,
                                                                      model, optimizer, global_step, param_optimizer)

            print("loss {}: {:.3f}".format(i, tr_loss / nb_tr_steps), file=sys.stderr)

            # Save a trained model
        model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self
        torch.save(model_to_save.state_dict(), output_model_file)
        if output_token_label_vocabulary is not None:
            torch.save(token_label_vocabulary, output_token_label_vocabulary)

    if args.do_predict:
        # Load a trained model that you have fine-tuned
        if args.word_segmentation is True or args.use_gold_segmentation_in_test is True:
            token_label_vocabulary = torch.load(output_token_label_vocabulary)
        model_state_dict = torch.load(output_model_file,
                                      map_location='cpu' if n_gpu == 0 or args.no_cuda is True else None)
        if args.chinese_zero is True:
            # TODO: Resolve import error
            raise ImportError
            # from pytorch_pretrained_bert.modeling_chinese_zero import BertForChineseZero
            # model = BertForChineseZero.from_pretrained(args.bert_model, state_dict=model_state_dict,
            #                                            num_expand_vocab=num_expand_vocab)
        else:
            model = BertForParsing.from_pretrained(args.bert_model, state_dict=model_state_dict,
                                                   num_expand_vocab=num_expand_vocab,
                                                   token_label_vocabulary=token_label_vocabulary,
                                                   parsing_algorithm=args.parsing_algorithm,
                                                   estimate_dep_label=args.estimate_dep_label)
        if args.do_train is False and num_finetuning_added_tokens > 0:
            add_vocab(finetuning_added_tokens, tokenizer, model)

        if vocab_size is None:
            vocab_size = model.config.vocab_size + num_finetuning_added_tokens
        model.to(device)
        if args.local_rank != -1:
            model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank],
                                                              output_device=args.local_rank)
        elif n_gpu > 1:
            model = torch.nn.DataParallel(model)

        # read examples
        while True:
            if args.chinese_zero is True:
                # TODO: Resolve import error
                raise ImportError
                # from chinese_zero import read_chinese_zero_examples, convert_examples_to_features_chinese_zero
                # eval_examples = read_chinese_zero_examples(input_file=args.predict_file, is_training=False)
            else:
                eval_examples = read_parsing_examples(
                    input_file=args.predict_file, is_training=False,
                    parsing=args.parsing,
                    word_segmentation=args.word_segmentation, pos_tagging=args.pos_tagging,
                    subpos_tagging=args.subpos_tagging, feats_tagging=args.feats_tagging,
                    use_gold_segmentation_in_test=args.use_gold_segmentation_in_test,
                    use_gold_pos_in_test=args.use_gold_pos_in_test,
                    h2z=args.h2z, knp_mode=args.knp_mode, multi_sentences=(not args.single_sentence))
                if len(eval_examples) == 0:
                    break

            if args.word_segmentation is True or args.use_gold_segmentation_in_test is True:
                for namespace in token_label_vocabulary:
                    token_label_vocabulary[namespace].add_indices(eval_examples)

            # convert examples to features
            if args.chinese_zero is True:
                # TODO: Resolve import error
                raise ImportError
                # eval_features = convert_examples_to_features_chinese_zero(
                #     examples=eval_examples,
                #     tokenizer=tokenizer,
                #     max_seq_length=args.max_seq_length,
                #     is_training=False,
                #     logger=logger)
            else:
                eval_features = convert_examples_to_features(
                    examples=eval_examples,
                    tokenizer=tokenizer,
                    max_seq_length=args.max_seq_length,
                    vocab_size=vocab_size,
                    is_training=False,
                    word_segmentation=args.word_segmentation,
                    use_gold_segmentation_in_test=args.use_gold_segmentation_in_test,
                    num_special_tokens=num_special_tokens,
                    special_tokens=special_tokens,
                )

            logger.info("***** Running predictions *****")
            logger.info("  Num orig examples = %d", len(eval_examples))
            logger.info("  Batch size = %d", args.predict_batch_size)

            all_input_ids = torch.tensor([f.input_ids for f in eval_features], dtype=torch.long)
            all_input_mask = torch.tensor([f.input_mask for f in eval_features], dtype=torch.long)
            all_segment_ids = torch.tensor([f.segment_ids for f in eval_features], dtype=torch.long)
            all_example_index = torch.arange(all_input_ids.size(0), dtype=torch.long)
            if args.chinese_zero is True:
                all_zps = pad_sequence([torch.tensor(f.zps, dtype=torch.long) for f in eval_features], batch_first=True,
                                       padding_value=-1)
                all_candidates_labels_set = get_all_pad_candidates_labels_set(eval_features)
                eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_example_index, all_zps,
                                          all_candidates_labels_set)
            else:
                eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_example_index)
            if args.local_rank == -1:
                eval_sampler = SequentialSampler(eval_data)
            else:
                eval_sampler = DistributedSampler(eval_data)
            eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.predict_batch_size)

            model.eval()
            all_results = []
            logger.info("Start evaluating")
            for input_ids, input_mask, segment_ids, example_indices, *rests in tqdm(eval_dataloader, desc="Evaluating"):
                if len(all_results) % 1000 == 0:
                    logger.info("Processing example: %d" % (len(all_results)))
                input_ids = input_ids.to(device)
                input_mask = input_mask.to(device)
                segment_ids = segment_ids.to(device)
                if args.chinese_zero is True:
                    all_zps = rests[0].to(device)
                    all_candidates_labels_set = rests[1].to(device)

                with torch.no_grad():
                    if args.chinese_zero is True:
                        ret_dict = model(input_ids, segment_ids, input_mask, all_zps, all_candidates_labels_set,
                                         is_training=False)
                    else:
                        ret_dict = model(input_ids, segment_ids, input_mask)
                for i, example_index in enumerate(example_indices):
                    heads, token_tags, topk_heads, topk_dep_labels = None, None, None, None
                    top_spans, antecedent_indices, predicted_antecedents = None, None, None
                    antecedent_labels_set = None
                    if args.chinese_zero is True:
                        antecedent_labels_set = ret_dict["antecedent_labels_set"][i].detach().cpu().tolist()
                    else:
                        heads = ret_dict["heads"][i].detach().cpu().tolist()
                        topk_heads = ret_dict["topk_heads"][i].detach().cpu().tolist()
                        if args.estimate_dep_label is True:
                            topk_dep_labels = ret_dict["topk_dep_labels"][i].detach().cpu().tolist()
                        if args.word_segmentation is True or args.use_gold_segmentation_in_test is True:
                            token_tags = {}
                            for namespace in ret_dict["token_tags"]:
                                token_tags[namespace] = ret_dict["token_tags"][namespace][i].detach().cpu().tolist()

                    eval_feature = eval_features[example_index.item()]
                    unique_id = int(eval_feature.unique_id)

                    all_results.append(RawResult(unique_id=unique_id,
                                                 heads=heads,
                                                 topk_heads=topk_heads,
                                                 topk_dep_labels=topk_dep_labels,
                                                 token_tags=token_tags,
                                                 top_spans=top_spans,
                                                 antecedent_indices=antecedent_indices,
                                                 predicted_antecedents=predicted_antecedents,
                                                 antecedent_labels_set=antecedent_labels_set))

            if args.prediction_result_filename == "-":
                # stdout
                output_prediction_file = None
            else:
                output_prediction_file = os.path.join(args.output_dir, args.prediction_result_filename)

            write_predictions(eval_examples, eval_features, all_results, output_prediction_file,
                              args.max_seq_length,
                              knp_dpnd, knp_case,
                              num_special_tokens=num_special_tokens,
                              special_tokens=special_tokens,
                              parsing=args.parsing,
                              word_segmentation=args.word_segmentation, pos_tagging=args.pos_tagging,
                              subpos_tagging=args.subpos_tagging, feats_tagging=args.feats_tagging,
                              estimate_dep_label=args.estimate_dep_label,
                              token_label_vocabulary=token_label_vocabulary,
                              use_gold_segmentation_in_test=args.use_gold_segmentation_in_test,
                              use_gold_pos_in_test=args.use_gold_pos_in_test,
                              chinese_zero=args.chinese_zero, knp_mode=args.knp_mode, output_tree=args.output_tree)
            if args.knp_mode is False:
                break


def preprocess_vocab(tokenizer, args):
    num_expand_vocab = 0
    num_finetuning_added_tokens = 0

    finetuning_added_tokens = None
    if args.finetuning_added_tokens is not None:
        finetuning_added_tokens = tuple(args.finetuning_added_tokens.split(","))
        num_finetuning_added_tokens = len(finetuning_added_tokens)
        num_expand_vocab += num_finetuning_added_tokens
        tokenizer.basic_tokenizer.never_split = tokenizer.basic_tokenizer.never_split + finetuning_added_tokens

    special_tokens = None
    num_special_tokens = 0
    if args.parsing is True:
        args.special_tokens = "[ROOT]"

    if args.special_tokens is not None:
        special_tokens = args.special_tokens.split(",")
        num_special_tokens = len(special_tokens)
        num_expand_vocab += num_special_tokens

    return num_expand_vocab, finetuning_added_tokens, num_finetuning_added_tokens, special_tokens, num_special_tokens


def add_vocab(finetuning_added_tokens, tokenizer, model):
    for i, finetuning_added_token in enumerate(finetuning_added_tokens):
        tokenizer.vocab[finetuning_added_token] = model.config.vocab_size + i
        logger.info("added vocab: {} ({})".format(finetuning_added_token, tokenizer.vocab[finetuning_added_token]))


def update_parameters(args, loss, n_gpu, tr_loss, step, nb_tr_steps, model, optimizer, global_step,
                      param_optimizer):
    if n_gpu > 1:
        loss = loss.mean()  # mean() to average on multi-gpu.
    if args.fp16 and args.loss_scale != 1.0:
        # rescale loss for fp16 training
        # see https://docs.nvidia.com/deeplearning/sdk/mixed-precision-training/index.html
        loss = loss * args.loss_scale
    if args.gradient_accumulation_steps > 1:
        loss = loss / args.gradient_accumulation_steps
    loss.backward()
    tr_loss += loss.item()
    nb_tr_steps += 1
    if (step + 1) % args.gradient_accumulation_steps == 0:
        if args.fp16 or args.optimize_on_cpu:
            if args.fp16 and args.loss_scale != 1.0:
                # scale down gradients for fp16 training
                for param in model.parameters():
                    if param.grad is not None:
                        param.grad.data = param.grad.data / args.loss_scale
            is_nan = set_optimizer_params_grad(param_optimizer, model.named_parameters(), test_nan=True)
            if is_nan:
                logger.info("FP16 TRAINING: Nan in gradients, reducing loss scaling")
                args.loss_scale = args.loss_scale / 2
                model.zero_grad()
                return tr_loss, nb_tr_steps, global_step
            optimizer.step()
            copy_optimizer_params_to_model(model.named_parameters(), param_optimizer)
        else:
            optimizer.step()
        model.zero_grad()
        global_step += 1

    return tr_loss, nb_tr_steps, global_step


def get_all_pad_candidates_labels_set(features):
    padded_candidate_label_sets = []
    for i, f in enumerate(features):
        padded_candidate_label_sets.append(pad_sequence(
            [torch.tensor((candidates_labels_set), dtype=torch.long) for candidates_labels_set in
             f.candidates_labels_set],
            batch_first=True, padding_value=-1))

    max_candidates_num = max([candidate_sets.size(1) for candidate_sets in padded_candidate_label_sets])

    all_pad_candidates_labels_set = pad_sequence([torch.cat(
        [s, torch.zeros(s.size(0), max_candidates_num - s.size(1), s.size(2), dtype=torch.long).fill_(-1)], dim=1) for s
        in padded_candidate_label_sets],
        batch_first=True, padding_value=-1)

    return all_pad_candidates_labels_set


def postprocess_args(args):
    if args.chinese_zero is True:
        args.lang = "zh"
    return args


if __name__ == "__main__":
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8')
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                        datefmt='%m/%d/%Y %H:%M:%S',
                        level=logging.INFO)

    main()
