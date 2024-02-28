import abc
import copy
import dataclasses
import functools
import logging
import os
import re
from dataclasses import dataclass
from functools import cached_property
from itertools import chain
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

import braceexpand
import datasets
import equinox as eqx
import fsspec
import jax
import numpy as np
import pyarrow as pa
import regex
from draccus import field
from jaxtyping import PRNGKeyArray
from tokenizers import normalizers

import haliax as hax
from haliax import Axis

from levanter.data.mixture import MixtureDataset, StopStrategy

# intercept the logging nonsense here
from levanter.logging import silence_transformer_nag  # noqa
from levanter.models.attention import AttentionMask
from levanter.models.lm_model import LmExample
from levanter.utils.hf_utils import num_cpus_used_by_tokenizer


silence_transformer_nag()  # noqa
from transformers import BatchEncoding, PreTrainedTokenizer, PreTrainedTokenizerBase, PreTrainedTokenizerFast  # noqa

from levanter.compat.hf_checkpoints import load_tokenizer  # noqa
from levanter.data._preprocessor import BatchProcessor, dict_from_record_batch  # noqa
from levanter.data.dataset import ShardableDataset  # noqa
from levanter.data.shard_cache import DEFAULT_ROWS_PER_CHUNK  # noqa
from levanter.data.shard_cache import CacheLedger  # noqa
from levanter.data.shard_cache import LEDGER_FILE_NAME as NEW_LEDGER_FILE_NAME  # noqa
from levanter.data.shard_cache import (  # noqa
    ChunkMetadata,
    LoggerMetricsMonitor,
    LoggingMetricsMonitor,
    MetricsMonitor,
    ShardCache,
    _serialize_json_and_commit,
    build_cache,
)
from levanter.data.sharded_dataset import ShardedDataset, TextUrlDataset, WrappedHFDataset  # noqa
from levanter.shapes import NamedShapeSpec, ShapeSpec  # noqa
from levanter.utils.jax_utils import use_cpu_device  # noqa


logger = logging.getLogger("levanter.data.text")

# TASKS:
# TODO: consider adding indexing a la Map-style datasets
# TODO: support seeking/serialization/restore in the dataset

LEDGER_FILE = "ledger.json"

DEFAULT_IGNORE_INDEX = -100  # Mirrors pytorch's default ignore index


class CausalLmDataset(ShardableDataset[LmExample]):
    def __init__(
        self,
        dataset: ShardableDataset[np.ndarray],
        QPos: Axis,
        KPos: Axis,
        fcm_prob: float = 0.0,
        key: Optional[PRNGKeyArray] = None,
        ignore_index: Optional[int] = None,
    ):
        self.dataset = dataset
        self.QPos = QPos
        self.KPos = KPos
        self.fcm_prob = fcm_prob
        self.key = key
        self.ignore_id = ignore_index

        if self.fcm_prob > 0.0 and self.key is None:
            raise ValueError("must provide key if fcm_prob > 0.0")

    def shard(self, shard_id: int, num_shards: int) -> "CausalLmDataset":
        return CausalLmDataset(
            self.dataset.shard(shard_id, num_shards), self.QPos, self.KPos, self.fcm_prob, self.key, self.ignore_id
        )

    def __iter__(self) -> Iterator[LmExample]:
        key = self.key
        sharding = jax.sharding.SingleDeviceSharding(jax.local_devices(backend="cpu")[0])

        with use_cpu_device():

            @functools.partial(eqx.filter_jit, out_shardings=sharding)
            def _create_lm_example(tokens, key):
                tokens = hax.named(tokens, self.QPos)

                example = LmExample.causal(tokens=tokens, ignore_id=self.ignore_id)

                if self.fcm_prob > 0:
                    # masks for attention
                    # We support forgetful causal masking (FCM) which is a technique that improves training speed by
                    # randomly masking out some of the context. This is a bit like dropout, but it's applied to the attention
                    # mask instead of the activations. It's described in https://arxiv.org/abs/2210.13432
                    assert self.key is not None
                    this_key, key = jax.random.split(key)
                    fcm_mask = hax.nn.attention.forgetful_causal_mask(self.KPos, self.fcm_prob, key=this_key)
                    attn_mask = example.attn_mask & AttentionMask.explicit(fcm_mask)
                    example = dataclasses.replace(example, attn_mask=attn_mask)

                return example

            for tokens in self.dataset:
                example = _create_lm_example(tokens, key)
                yield example


class TokenSeqDataset(ShardableDataset[np.ndarray]):
    """
    A dataset that yields sequences of tokens of fixed length from a TokenizedDocumentCache.

    :param doc_cache: the TokenizedDocumentCache to draw from
    :param seq_len: The max length of sequences to emit
    """

    def __init__(self, doc_cache, seq_len: int, stride: Optional[int] = None):
        self.doc_cache = doc_cache
        self.seq_len = seq_len
        self.stride = stride

    def shard(self, shard_id: int, num_shards: int) -> "TokenSeqDataset":
        """
        Split the dataset into num_processes shards.
        """
        return TokenSeqDataset(self.doc_cache.shard(shard_id, num_shards), self.seq_len, self.stride)

    def __iter__(self) -> Iterator[np.ndarray]:
        extra_tokens = None  # BatchEncoding of the last tokens from the previous doc
        for doc in self.doc_cache:
            # TODO: we could be cleverer here, and avoid these expensive copies etc
            # should run some benchmarks to see if it's worth it
            if extra_tokens is not None:
                doc = _stack_batch_encodings(extra_tokens, doc)
                extra_tokens = None

            for encoded_slice in concatenate_and_group_texts(doc, self.seq_len, self.stride, drop_remainder=False):
                if len(encoded_slice["input_ids"]) < self.seq_len:
                    assert extra_tokens is None
                    extra_tokens = encoded_slice
                else:
                    extra_tokens = None
                    ids = encoded_slice["input_ids"]
                    yield ids

    @staticmethod
    def load(seq_len: int, cache_dir: str, stride: Optional[int] = None) -> "TokenSeqDataset":
        doc_cache = TokenizedDocumentCache.load(cache_dir, True)
        return TokenSeqDataset(doc_cache, seq_len, stride)


class BatchEncodingDataset(ShardableDataset[BatchEncoding]):
    """
    A Dataset that yields HF BatchEncodings from a ShardCache.
    This basically yields a dict-of-arrays, just the HF BatchEncoding class version of dict.
    """

    def __init__(self, cache: ShardCache, return_batches: bool = False):
        self.cache = cache
        self.return_batches = return_batches

    def __iter__(self) -> Iterator[BatchEncoding]:
        for batch in self.cache:
            encoding = _batch_encoding_from_record_batch(batch, flatten_docs=False)
            if self.return_batches:
                yield encoding
            else:
                batch_size = 0
                for v in encoding.values():
                    batch_size = len(v)
                    break

                for i in range(batch_size):
                    # this doesn't work for reconstituted batches, so we have to do this
                    # I have no idea why this is the case
                    #     yield encoding[i]
                    yield BatchEncoding({k: v[i] for k, v in encoding.items()})

    def shard(self, shard_id: int, num_shards: int) -> "BatchEncodingDataset":
        return BatchEncodingDataset(self.cache.shard(shard_id, num_shards))

    @staticmethod
    def load(cache_dir: str, return_batches: bool = False, batch_size: Optional[int] = None) -> "BatchEncodingDataset":
        if batch_size is None:
            batch_size = 1
        cache = ShardCache.load(cache_dir, batch_size=batch_size)
        return BatchEncodingDataset(cache, return_batches=return_batches)


class TokenizedDocumentCache(ShardableDataset[BatchEncoding]):
    """
    Represents a tokenized document cache, which is a directory of parquet files with a ledger file.

    The difference between this class and the TokenSeqDataset is that this class yields entire documents,
    while the TokenSeqDataset yields tokens sequences of fixed length from concatenated documents.
    """

    def __init__(self, chunk_cache: ShardCache, flatten_docs):
        self.chunk_cache = chunk_cache
        self.flatten_docs = flatten_docs

    def __iter__(self):
        """Reads the cache files produced by cache_and_group and yields tokenized sequences.
        If flatten is false, this returns the docs as they were presented to the caching process. If flatten is True,
        then the documents returned are actually concatenated documents, where the number is the number of documents
        presented as a batch to the caching process."""
        for batch in self._chunks():
            yield _batch_encoding_from_record_batch(batch, self.flatten_docs)

    def _chunks(self):
        return self.chunk_cache.iter_batches_from_chunks()

    @staticmethod
    def build_or_load(
        cache_dir,
        source: ShardedDataset[str],
        tokenizer: PreTrainedTokenizerBase,
        flatten_docs=True,
        enforce_eos=True,
        batch_size=128,
        rows_per_chunk=DEFAULT_ROWS_PER_CHUNK,
        monitors=None,
        await_finished=True,
        override_resources=None,
    ) -> "TokenizedDocumentCache":
        bt = BatchTokenizer(tokenizer, enforce_eos=enforce_eos, override_resources=override_resources)
        monitors = monitors or []
        cache = build_cache(
            cache_dir,
            source,
            bt,
            await_finished=await_finished,
            batch_size=batch_size,
            rows_per_chunk=rows_per_chunk,
            monitors=monitors,
        )
        if cache.is_finished:
            logger.info(f"Cache {cache_dir} is complete.")
        else:
            logger.info(
                f"Cache {cache_dir} is incomplete. This will block until at least one chunk per process is complete."
            )

        return TokenizedDocumentCache(cache, flatten_docs=flatten_docs)

    @staticmethod
    def load(cache_dir, batch_size: int = 128, flatten_docs=True):
        """
        Load a TokenizedDocumentCache from a directory. If the ledger file is not present, this will raise a
        FileNotFoundError.

        NOTE: ATM this attempts to migrate old caches to the new format, but this will be removed in the future.

        :param cache_dir:
        :param flatten_docs: If true, then multiple documents from a single batch (when the cache was built) will be
        concatenated into a single document. Often one is concatenating documents anyway, so this is a useful option.
        :return:
        """

        try:
            cache = ShardCache.load(cache_dir, batch_size=batch_size)
            return TokenizedDocumentCache(cache, flatten_docs=flatten_docs)
        except FileNotFoundError:
            raise FileNotFoundError(f"{cache_dir} is not a complete cache")
        except Exception:
            logger.exception("error loading cache")
            raise

    def shard(self, shard_index, num_shards):
        if num_shards <= shard_index:
            raise ValueError(f"Shard index {shard_index} is out of range")

        if num_shards == 1:
            return self

        return TokenizedDocumentCache(self.chunk_cache.shard(shard_index, num_shards), self.flatten_docs)


def _batch_encoding_from_record_batch(b: pa.RecordBatch, flatten_docs: bool):
    if flatten_docs:
        # insert a newaxis to the beginning so that it appears to be bs=1
        return BatchEncoding(
            {
                b.field(i).name: b.column(i).values.to_numpy(zero_copy_only=False)[np.newaxis, :]
                for i in range(b.num_columns)
            },
        )
    else:
        return BatchEncoding(dict_from_record_batch(b))


def _maybe_force_tokenizer_parallelism(tokenizer: PreTrainedTokenizerBase):
    if tokenizer.is_fast and os.getenv("TOKENIZERS_PARALLELISM") is None:
        # if we're using a fast tokenizer, we want to force parallelism
        # to be the number of CPUs
        os.environ["TOKENIZERS_PARALLELISM"] = "true"


LONG_STRING_WORKAROUND = 100_000
MAX_SENTENCE_LEN = 1024

ws = regex.compile(r"\s")


class BatchTokenizer(BatchProcessor[str]):
    """
    A batch processor that tokenizes a batch of strings using a tokenizer.
    By default, this will append eos to the end of the string, even if the tokenizer doesn't.
    """

    def __init__(
        self,
        tokenizer: PreTrainedTokenizerBase,
        enforce_eos=True,
        *,
        batch_size=128,
        override_resources=None,
        _workaround_len=LONG_STRING_WORKAROUND,
        _max_sentence_len=MAX_SENTENCE_LEN,
    ):
        _maybe_force_tokenizer_parallelism(tokenizer)
        self.tokenizer = tokenizer
        self.override_resources = override_resources

        # see if the tokenizer appends eos
        # HF's BPE-based tokenizers do not, but the bert and roberta ones do
        # TODO: this doesn't necessarily ensure it, I guess, but eh
        if enforce_eos:
            input_ids = tokenizer("hi there")["input_ids"]
            should_append_eos = input_ids[-1] != tokenizer.eos_token_id
        else:
            should_append_eos = False

        self._batch_size = batch_size

        self._need_to_add_eos = should_append_eos
        self._workaround_len = _workaround_len
        self._vocab_size = tokenizer.vocab_size
        self._eos_token_id = tokenizer.eos_token_id
        self._max_sentence_len = _max_sentence_len

    def __call__(self, batch: Sequence[str]) -> BatchEncoding:
        # print("batch len", len(batch))
        # print("batch[0]", batch[0])
        #print("tokenizing batch ", batch)

        # break strings at the sentence level
        orig_batch = batch
        batch = []
        needs_merge = []
        wc = []
        for i, d in enumerate(orig_batch):
            # Replace all instances of ." with ".
            # This is a bit of a hack, but it's a common enough error that it's worth doing
            d = d.replace('."', '".')
            d = d.replace(".'", "'.")
            d = d.replace('?"', '"?')
            d = d.replace('!"', '"!')
            d = d.replace("?'", "'?")
            d = d.replace("!'", "'!")
            # If d doesn't end with a sentence terminator, ignore it
            if (d[-1] == "\n" and d[:-1].split()[-1][-1] not in ".!?") or (d.split()[-1][-1] not in ".!?"):
                continue
            split_sentences = re.split(r'(?<=[.!?])\s+', d)
            sentences = []
            for i in range(len(split_sentences)):
                if len(split_sentences[i]) > 0:
                    if not split_sentences[i].startswith(" "):
                        split_sentences[i] = " " + split_sentences[i]
                    sentences.append(split_sentences[i])
            word_counts = [len(s.split()) for s in sentences] # could alternatively try token counts
            # if sentences[len(sentences) - 1].startswith(self.tokenizer.eos_token):
            #     sentences[len(sentences) - 2] += sentences[len(sentences) - 1]
            #     sentences.pop()
            # print("original batch:", d)
            # print("word counts:", word_counts)

            if sentences: # if we didn't delete everything in d
                # print("sentences:", sentences)
                batch.extend(sentences)
                wc.extend(word_counts)
                needs_merge.append(False)
                needs_merge.extend([True] * (len(sentences) - 1))

                if self._need_to_add_eos:
                    # print("adding")
                    batch.extend([self.tokenizer.eos_token])
                    wc.append(1)
                    needs_merge.append(True)

            if self._needs_long_sequence_workaround:
                for sentence in sentences:
                    if len(sentence) > self._workaround_len:
                        print("Need a workaround because sentence exceeds length ", self._workaround_len)
        # orig_batch = batch
        # batch = []
        # for i, d in enumerate(orig_batch):

        encoding = self.tokenizer(batch, return_attention_mask=False, verbose=False)  # type: ignore

        # print("needs merge", needs_merge)
        # print("vocab size", self._vocab_size, "eos token id", self._eos_token_id, "encoding", encoding)
        #print("encoded batch[0] as:", encoding[0])
        #print("adding encoding[0] and encoding[1]:", encoding[0] + )

        if needs_merge:
            new_encoding = self._merge_split_encodings(batch, encoding, needs_merge, wc, eos_token_id=self._eos_token_id, max_sentence_len=self._max_sentence_len)
            encoding = BatchEncoding(new_encoding)

        return encoding

    @staticmethod
    def _merge_split_encodings(batch, encoding, needs_merge, wc, eos_token_id, max_sentence_len):
        # merge the encodings back together
        # we might need to merge multiple encodings together
        # needs merge marks the first n-1 encodings that need to be merged for each document
        new_encoding = {}
        # print("performing merge")
        # print("needs_merge len", len(needs_merge))
        # print("word counts len", len(wc))
        # print("batch len", len(batch))
        # print("encoding len", len(encoding.items()))
        for k, v in encoding.items():
            if len(v) == 0:
                continue
            if isinstance(v[0], np.ndarray):
                assert len(v) == len(batch)
                v_out = []
                vs_to_merge = []
                for i in range(len(batch)):
                    if not needs_merge[i]:
                        v_out.append(np.concatenate(vs_to_merge))
                        vs_to_merge = []
                    sentence_len = wc[i] + eos_token_id + 1 # eos_token_id + 2 means 1 sentence length, eos_token_id + 1 is reserved for special case
                    if wc[i] < max_sentence_len and v[i][0] != eos_token_id:
                        # ignore special case of "sentence is too long to fit in max_sentence_len (consider using a special character)?
                        # also don't append sentence length if it's just the eos token
                        # print("word count", wc[i])
                        # print("sentence len", sentence_len)
                        # print("sentence", v[i])
                        v[i] = [sentence_len] + v[i]
                    vs_to_merge.append(v[i])

                if len(vs_to_merge) > 0:
                    v_out.append(np.concatenate(vs_to_merge))

                new_encoding[k] = v_out
            elif isinstance(v[0], list):
                v_out = []
                vs_to_merge = []
                for i in range(len(batch)):
                    if not needs_merge[i]:
                        if len(vs_to_merge) > 0:
                            v_out.append(list(chain(*vs_to_merge)))
                        vs_to_merge = []
                    sentence_len = wc[i] + eos_token_id + 1 # eos_token_id + 2 means 1 sentence length, eos_token_id + 1 is reserved for special case
                    if wc[i] < max_sentence_len and v[i][0] != eos_token_id:
                        # ignore special case of "sentence is too long to fit in max_sentence_len (consider using a special character)?
                        # also don't append sentence length if it's just the eos token
                        # print("word count", wc[i])
                        # print("sentence len", sentence_len)
                        # print("sentence", v[i])
                        v[i] = [sentence_len] + v[i]
                    # print("type v[i]", type(v[i]))
                    # print("type v[i][0]", type(v[i][0]))
                    vs_to_merge.append(v[i])

                if len(vs_to_merge) > 0:
                    v_out.append(list(chain(*vs_to_merge)))
                new_encoding[k] = v_out
            else:
                raise ValueError(f"Unknown type {type(v[0])}")
        return new_encoding

    # TODO remove this when it's resolved https://github.com/huggingface/tokenizers/issues/1449
    @cached_property
    def _needs_long_sequence_workaround(self):
        if isinstance(self.tokenizer, PreTrainedTokenizerFast):
            normalizer = self.tokenizer.backend_tokenizer.normalizer
            if normalizer is None:
                return False
            # if there's a "Replace" normalizer, then we need to do the workaround
            # inexplicably there's no way to see inside a Sequence so we also have to assume it needs it
            return isinstance(normalizer, (normalizers.Replace, normalizers.Sequence))
        else:
            return False

    @property
    def num_cpus(self) -> int:
        if self.override_resources is not None:
            cpus = self.override_resources.get("num_cpus", None)
            if cpus is not None:
                return cpus
        return num_cpus_used_by_tokenizer(self.tokenizer)

    @property
    def num_gpus(self) -> int:
        if self.override_resources is not None:
            return self.override_resources.get("num_gpus", 0)
        return 0

    @property
    def batch_size(self) -> int:
        return self._batch_size


def concatenate_and_group_texts(
    encoding: BatchEncoding,
    seq_len: int,
    stride: Optional[int] = None,
    drop_remainder: bool = True,
    mask_stride_overlap=True,
) -> Iterator[BatchEncoding]:
    """Groups texts in a batch together. Typically, you'll want to use this with a fairly large
    set of texts, e.g. 1000 docs.

    You should set mask_stride_overlap to True and drop_remainder to False if you want to use this for test data

    Args:
        encoding: The batch of texts to concatenate and group.
        seq_len: The max length of sequences to emit
        stride: The stride to use when grouping texts. If None, then the stride is set to seq_len.
        mask_stride_overlap: Whether to mask out overlapping tokens if we're using a stride.
        drop_remainder: Whether to drop the last batch if it's not a multiple of the seq_len.

    Returns:
        An iterator of tokenized texts, one at a time.
    """
    concatenated = BatchEncoding(data={k: np.array(list(chain(*v))) for k, v in encoding.items()})
    total_length = len(concatenated.input_ids)
    stride = stride or seq_len

    # Drop the "very last" bit of the dataset that doesn't fit into block size...
    if drop_remainder and total_length % stride != 0:
        total_length = ((total_length - seq_len + stride) // stride) * stride

    # Split by Chunks of Maximum Length
    # we want to take chunks up until we've covered all "total_length" tokens with a sliding window of size "stride"
    for begin in range(0, total_length - seq_len + stride, stride):
        data = {k: v[begin : begin + seq_len] for k, v in concatenated.items()}

        if mask_stride_overlap and stride != seq_len:
            labels = data.get("labels", data["input_ids"])
            if begin != 0:
                labels = _mask_overlap(labels, seq_len, stride)
            data["labels"] = labels

        yield BatchEncoding(data=data)


# -100 is pytorch's label mask
def _mask_overlap(labels, target_len, stride, sentinel=-100):
    """Masks out overlapping tokens in a sequence when we're using a stride."""
    labels = copy.deepcopy(labels)
    if isinstance(labels, list):
        for i in range(target_len - stride):
            if i < len(labels):
                labels[i] = sentinel
    else:
        labels[0 : target_len - stride] = sentinel

    return labels


def _stack_batch_encodings(a: BatchEncoding, b: BatchEncoding) -> BatchEncoding:
    """Stacks two batch encodings together, assuming that the keys are the same."""

    def _ensure_batched(x):
        if len(x) == 0:
            return list(x)
        elif isinstance(x[0], Sequence) or isinstance(x[0], np.ndarray):
            return list(x)
        else:
            return [x]

    return BatchEncoding({k: _ensure_batched(a[k]) + _ensure_batched(b[k]) for k in a.keys()})


@dataclass
class LMDatasetSourceConfig:
    """This class represents a dataset source with URLs or hf name/id."""

    id: Optional[str] = None  # id (or path) for hf dataset
    name: Optional[str] = None  # name for hf dataset

    plaintext: bool = False
    stream: bool = True  # whether to use streaming when doing hf
    text_key: str = "text"  # key for the text field in the jsonl file or hf dataset

    train_urls: List[str] = ()  # type: ignore
    validation_urls: List[str] = ()  # type:ignore

    def get_shard_source(self, split) -> Optional[ShardedDataset[str]]:
        if self.id is not None:
            try:
                ds = WrappedHFDataset(self.id, split=split, name=self.name, streaming=self.stream)
            except ValueError as e:
                # if the message starts with Bad split, then just return None
                if str(e).startswith("Bad split"):
                    logger.warning(f"Splits {split} not found for {self.id} {self.name}")
                    return None
                else:
                    raise

            if len(ds.shard_names) == 0:
                return None

            return ds.map(lambda x: x[self.text_key])
        else:
            split_urls = self.urls_for_split(split)
            if len(split_urls) == 0:
                return None
            return TextUrlDataset(split_urls, self.text_key)

    def doc_iterator(self, split: str):
        if self.id is not None:
            dataset = datasets.load_dataset(self.id, name=self.name, streaming=self.stream)
            data = dataset[split]
            for doc in data:
                yield doc[self.text_key]
        else:
            urls = self.urls_for_split(split)

            yield from TextUrlDataset(urls, self.text_key)

    def urls_for_split(self, split):
        if split == "train":
            urls = self.train_urls
        elif split == "validation":
            urls = self.validation_urls
        else:
            raise ValueError(f"Unknown split {split}")

        def fsspec_expand_glob(url):
            if "*" in url:
                fs = fsspec.core.url_to_fs(url)[0]
                return fs.glob(url)
            else:
                return [url]

        urls = [globbed for pat in urls for url in braceexpand.braceexpand(pat) for globbed in fsspec_expand_glob(url)]
        return urls


@dataclass
class LMTaskConfig(abc.ABC):
    tokenizer: str = "gpt2"
    vocab_size: Optional[int] = None  # if using the passthrough tokenizer, this is required

    # config related to caching
    cache_dir: str = "cache/"
    rows_per_chunk: int = DEFAULT_ROWS_PER_CHUNK  # number of rows to process and cache per chunk
    enforce_eos: bool = True  # whether to append eos even if the tokenizer doesn't

    ignore_token_id: Optional[int] = None

    @cached_property
    def the_tokenizer(self) -> PreTrainedTokenizerBase:
        if self.tokenizer == "passthrough":
            return PassthroughTokenizer(self.vocab_size)
        else:
            return load_tokenizer(self.tokenizer)

    @abc.abstractmethod
    def train_set(
        self, seq_len: int, monitors: Union[bool, List[MetricsMonitor]] = True
    ) -> ShardableDataset[np.ndarray]:
        pass

    @abc.abstractmethod
    def validation_sets(
        self, seq_len: int, monitors: Union[bool, List[MetricsMonitor]] = True
    ) -> Mapping[str, ShardableDataset[np.ndarray]]:
        pass


@dataclass
class LMDatasetConfig(LMDatasetSourceConfig, LMTaskConfig):
    """This class supports loading data both from HF Datasets and from a raw dataset of jsonl urls"""

    def train_set(
        self, seq_len: int, monitors: Union[bool, List[MetricsMonitor]] = True
    ) -> ShardableDataset[np.ndarray]:
        ds = self.token_seq_dataset("train", seq_len, monitors)
        if ds is None:
            raise ValueError("No training set!")
        return ds

    def validation_set(
        self, seq_len: int, monitors: Union[bool, List[MetricsMonitor]] = True
    ) -> Optional[TokenSeqDataset]:
        return self.token_seq_dataset("validation", seq_len, monitors)

    def validation_sets(
        self, seq_len: int, monitors: Union[bool, List[MetricsMonitor]] = True
    ) -> Mapping[str, ShardableDataset[np.ndarray]]:
        validation_set = self.validation_set(seq_len, monitors)
        if validation_set is not None:
            return {"": validation_set}
        else:
            return {}

    @cached_property
    def _has_validation_set(self):
        if len(self.validation_urls) > 0:
            return True

        if self.id is not None:
            dataset = datasets.load_dataset(self.id, name=self.name, streaming=self.stream, split="validation")
            try:
                next(iter(dataset))
                return True
            except StopIteration:
                return False

        return False

    def token_seq_dataset(
        self, split: str, seq_len: int, monitors: Union[bool, List[MetricsMonitor]] = True
    ) -> Optional[TokenSeqDataset]:
        cache = self.build_or_load_cache(split, monitors=monitors)
        if cache is None:
            return None
        return TokenSeqDataset(cache, seq_len)

    def build_or_load_cache(
        self, split: str, monitors: Union[bool, List[MetricsMonitor]] = True, logger_name: Optional[str] = None
    ) -> Optional[TokenizedDocumentCache]:
        split_cache_dir = os.path.join(self.cache_dir, split)
        name = logger_name or os.path.basename(self.cache_dir)

        try:
            return TokenizedDocumentCache.load(split_cache_dir, flatten_docs=True)
        except FileNotFoundError:
            pass

        source = self.get_shard_source(split)
        if source is None:
            logger.info(f"No data for {split}")
            return None

        logger.info(f"Building cache for {split}...")

        if monitors is True:
            monitors = [
                LoggingMetricsMonitor(prefix=f"preprocessing/{name}/{split}", commit=False),
                LoggerMetricsMonitor(f"preprocessing.{name}.{split}"),
            ]
        elif monitors is False:
            monitors = []

        return TokenizedDocumentCache.build_or_load(
            split_cache_dir,
            source,
            self.the_tokenizer,
            enforce_eos=self.enforce_eos,
            flatten_docs=True,
            rows_per_chunk=self.rows_per_chunk,
            monitors=monitors,
            # TODO: it would be better if we could just prioritize validation higher (we typically want it after the first grad step)
            await_finished=(split == "validation"),
        )


class PassthroughTokenizer(PreTrainedTokenizer):
    def __init__(self, vocab_size, **kwargs):
        self._vocab = {i: i for i in range(vocab_size)}
        self._vocab_size = vocab_size
        super().__init__(**kwargs)

    @property
    def vocab_size(self) -> int:
        return self._vocab_size

    def get_vocab(self):
        return self._vocab

    def save_vocabulary(self, save_directory: str, filename_prefix: Optional[str] = None) -> Tuple[str, ...]:
        return ()

    def _tokenize(self, text, **kwargs):
        tokens = np.fromstring(text, dtype=int, sep=" ")
        return tokens

    def _convert_token_to_id(self, token: str) -> int:
        return int(token)

    def _convert_id_to_token(self, index: int) -> str:
        return str(index)


@dataclass
class LMMixtureDatasetConfig(LMTaskConfig):
    """This class represents a mixture of datasets with their associated weights."""

    # data source configs and weights
    configs: Dict[str, LMDatasetSourceConfig] = field(default_factory=dict)
    """ configuration of each dataset source (urls, hf dataset id, etc.) """
    train_weights: Dict[str, float] = field(default_factory=dict)
    """ weights for each dataset source. They will be normalized to sum to 1. """
    stop_strategy: str = field(default=StopStrategy.FIRST_STOP_STRATEGY)

    def __post_init__(self):
        if len(self.configs) == 0:
            raise ValueError("At least one dataset must be provided")

        if set(self.configs.keys()) != set(self.train_weights.keys()):
            raise ValueError(
                f"The keys in configs and weights must be the same;got {self.configs.keys()} and"
                f" {self.train_weights.keys()}"
            )

    def train_set(
        self, seq_len: int, monitors: Union[bool, List[MetricsMonitor]] = True
    ) -> ShardableDataset[np.ndarray]:
        doc_caches = self.build_caches("train", monitors=monitors)
        token_datasets = {name: TokenSeqDataset(cache, seq_len, stride=None) for name, cache in doc_caches.items()}
        return MixtureDataset(datasets=token_datasets, weights=self.train_weights, stop_strategy=self.stop_strategy)

    def training_sets(
        self, seq_len: int, monitors: Union[bool, List[MetricsMonitor]] = True
    ) -> Mapping[str, ShardableDataset[np.ndarray]]:
        doc_caches = self.build_caches("train", monitors=monitors)
        token_datasets = {name: TokenSeqDataset(cache, seq_len, stride=None) for name, cache in doc_caches.items()}
        return token_datasets

    def validation_sets(
        self, seq_len: int, monitors: Union[bool, List[MetricsMonitor]] = True
    ) -> Mapping[str, ShardableDataset[np.ndarray]]:
        doc_caches = self.build_caches("validation", monitors=monitors)
        token_datasets = {name: TokenSeqDataset(cache, seq_len, stride=None) for name, cache in doc_caches.items()}
        return token_datasets

    def build_caches(
        self, split: str, monitors: Union[bool, List[MetricsMonitor]] = True
    ) -> Dict[str, TokenizedDocumentCache]:
        # this is a bit gross, but we want to forward all "Task" config fields to the LMDatasetConfig for building.
        # We do this by just grabbing all the fields from the LMDatasetConfig and forwarding them to the
        # LMDatasetConfig.build_or_load_cache method. We exclude the cache_dir field.
        task_config_fields = set(x.name for x in dataclasses.fields(LMTaskConfig))
        task_config_dict = {k: v for k, v in self.__dict__.items() if k in task_config_fields and k != "cache_dir"}

        caches = {}
        for name, source_config in self.configs.items():
            weight = self.train_weights.get(name, 0)

            if weight == 0 and split == "train":
                continue

            source_config_dict = source_config.__dict__

            dataset = LMDatasetConfig(
                cache_dir=os.path.join(self.cache_dir, name),
                **source_config_dict,
                **task_config_dict,
            )
            cache = dataset.build_or_load_cache(split, monitors)
            # drop the data source and corresponding weight if the cache is not built
            if cache is None:
                logger.warning(f"Skipping {name} for split {split} because no source was provided")
            else:
                caches[name] = cache
        return caches
