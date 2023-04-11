from typing import Generic, Type, TypeVar

import equinox as eqx
import jax

import haliax
from haliax import Axis


M = TypeVar("M", bound=eqx.Module)


class Stacked(eqx.Module, Generic[M]):
    """
    A "Stacked" wraps another module and produces a "stacked" version of it, where an input is applied
    to each instance of the stacked module in sequence. This is useful for e.g. transformers
    where you have multiple instances of the same transformer block and the input is applied in a fold/for loop
    in sequence.

    It's similar in spirit to an equinox.nn.Sequential, but it must be homogeneous. In Jax, this is much cheaper
    to compile than a sequential (or moral equivalent), because jax compiles the module's method once, instead of
    unrolling the sequential and compiling everything as a giant graph.
    """

    stacked: M
    Block: Axis = eqx.static_field()
    # TODO: support fancier gradient checkpointing
    gradient_checkpointing: bool = eqx.static_field()

    def __init__(self, Block: Axis, module: Type[M], *args, gradient_checkpointing: bool = False, **kwargs):
        super().__init__()
        self.Block = Block
        self.stacked = haliax.vmap(module, Block)(*args, **kwargs)
        self.gradient_checkpointing = gradient_checkpointing

    def scan(self, init, *extra_args, **extra_kwargs):
        if self.gradient_checkpointing:
            do_block = jax.checkpoint(self._do_block)
        else:
            do_block = self._do_block
        return haliax.scan(do_block, self.Block)(init, self.stacked, *extra_args, **extra_kwargs)

    def fold(self, init, *args, **kwargs):
        if self.gradient_checkpointing:
            do_block = jax.checkpoint(self._do_block)
        else:
            do_block = self._do_block

        return haliax.fold(do_block, self.Block)(init, self.stacked, *args, **kwargs)

    @staticmethod
    def _do_block(carry, block, *extra_args, **extra_kwargs):
        return block(carry, *extra_args, **extra_kwargs)

    def __call__(self, *args, **kwargs):
        return self.fold(*args, **kwargs)