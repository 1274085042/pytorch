from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Optional, Set, Union

import torch
from torch._subclasses import FakeTensor
from torch._subclasses.fake_tensor import is_fake

from .. import config

MutationType = Enum(
    "MutationType", ("none", "metadata_only", "data", "data_and_metadata")
)
OutputType = Enum(
    "OutputType",
    (
        # output is not an alias
        "non_alias",
        # output aliases an input
        "alias_of_input",
        # output **is** an input tensor
        "is_input",
        # output has a ._base tensor, which is a graph intermediate.
        # We need to return its ._base as a graph output,
        # so its requires_grad info is populated correctly.
        # Instructs the runtime code to regenerate the current output
        # from a base tensor, graph_intermediates[base_idx]
        "alias_of_intermediate_save_as_output",
        # Same as above; but we don't need to explicitly add its ._base
        # as a graph output, because it already **is** a graph output.
        "alias_of_intermediate",
        # Same as above; but the output's ._base is **already** a user output.
        # Instructs the runtime code to regenerate the current output from
        # a base tensor, user_outputs[base_idx]
        "alias_of_intermediate_base_is_user_output",
        # See Note [Intermediate Bases Optimization]
        "unsafe_view_alias",
        # output is an alias, but has a custom autograd.Function backward.
        # In this case, we don't want to do view-replay, since we won't be able to replay the custom function.
        # Instead, we'll treat this output "normally", and trace its backward into the graph.
        "custom_function_view",
    ),
)


# This class stores info about every user output.
@dataclass(frozen=True)
class OutputAliasInfo:
    # Tells us if this output is:
    # (1) a regular (non-aliased) output
    # (2) an alias of a forward input
    # (3) **is** a forward input (special case of "alias_of_input")
    # (4) an alias of an intermediate (aka an alias of an output of the inner traced forward)
    # (5) an alias of an intermediate, that explicitly requires returning the intermediate
    #     as a graph output
    # (6) an alias of an intermediate, where that intermediate is also a user output
    output_type: OutputType
    # The raw type of the output (torch.Tensor, SymInt, etc)
    raw_type: type
    # If (1) above, then
    # - base_idx is None
    # If (2) or (3) above, then
    # - Tells us that the base of this alias is user_fwd_input[base_idx]
    #   (This is an index into the inputs *before* we make synthetic bases)
    # If (4) or (5) above, then
    # - Tells us that the base of this alias is output_graph_intermediates[base_idx]
    #   here, this refers to the index of the *direct* traced
    # If (6) above, then:
    # - Tells us that the base of this alias is output_user_fwds[base_idx]
    #   here, this refers to the index of the *direct* traced
    base_idx: Optional[int]
    # If it is a Tensor, what the dynamic dims are (otherwise is None)
    dynamic_dims: Optional[Set[int]]
    # requires_grad
    requires_grad: bool


class MutationType(Enum):
    NOT_MUTATED = 1
    MUTATED_IN_GRAPH = 2
    MUTATED_OUT_GRAPH = 3


# This class tells us info about user inputs.
@dataclass(frozen=True)
class InputAliasInfo:
    is_leaf: bool
    mutates_data: bool
    mutates_metadata: bool
    mutations_hidden_from_autograd: bool
    requires_grad: bool
    mutation_type: MutationType


@dataclass
class SubclassCreationMeta:
    """
    Used for AOTDispatch.
    This dataclass gives us the information we need to reconstruct a tensor subclass
    from our flat inputs.
    Why is this important? The graph that we'd like to trace out contains flat tensor inputs,
    But the user's original model may have subclass inputs and outputs.
    So we need to wrap/unwrap subclasses as necessary to translate between the user's
    view (subclass inps/outs), and the backend compiler's view (graph with no subclass args).

    Complications arise mostly from the fact that a subclass can hold more than one inner tensor;
    So for a given subclass input/output, we need to carefully track which indices map
    to the subclass tensor in the corresponding "dense-tensor-only" graph.
    """

    # In the inner graph that only takes in dense tensor inputs,
    # this maps to the first index of "tensors that should go in this subclass wrapper"
    flat_tensor_start_idx: int
    # The number of tensors that live in this subclass wrapper
    arg_count: int
    # Stores the original subclass itself.
    # This is needed because we need the autograd metadata on the original subclass
    # (this is guaranteed to be a wrapper subclass that holds a fake tensor,
    #  so holding onto this at runtime shouldn't leak memory)
    original_subclass: torch.Tensor
    # meta and inner_keys are produced by the subclass's __tensor_flatten__.
    # We need to keep them around to plumb them into __tensor_unflatten__.
    meta: Any
    inner_keys: List[any]

    def creation_fn(self, all_args, *, is_runtime: bool):
        curr_args = all_args[
            self.flat_tensor_start_idx : self.flat_tensor_start_idx + self.arg_count
        ]
        assert len(curr_args) == len(
            self.inner_keys
        ), f"inner_keys: {str(self.inner_keys)}. len(curr_args): {len(curr_args)}"
        out = type(self.original_subclass).__tensor_unflatten__(
            dict(zip(self.inner_keys, curr_args)), self.meta
        )
        if not is_runtime:
            # After wrapping up the inner dense tensors into a subclass, we need to make sure that our new wrapper
            # has correct autograd metadata, since we'll be tracing through the autograd engine with the subclass.
            # We don't trace through the autograd engine at runtime though, so no need
            # to compute this extra metadata then!
            torch._mirror_autograd_meta_to(self.original_subclass, out)

        return out

    def __post_init__(self):
        # sanity assert to make sure we don't leak memory
        assert is_fake(self.original_subclass)


# This class encapsulates all aliasing + mutation info we need about the forward graph
# See a more detailed overview of the edge case handling at
# https://docs.google.com/document/d/19UoIh_SVrMy_b2Sx5ZaeOJttm6P0Qmyss2rdBuyfoic/edit
@dataclass(eq=False)
class ViewAndMutationMeta:
    # length = # user inputs
    # This gives us info about every input, and what sort of mutation happened to it (if any)
    input_info: List[InputAliasInfo]

    # length = # user outputs
    # This gives us info about every output (mostly around whether it aliases other tensors)
    output_info: List[OutputAliasInfo]

    # length = the number of intermediate bases appended as outputs to the end of the forward graph.
    # Note: this is not necessarily the same thing as:
    #   len([x for x in output_info if x.output_type == OutputType.alias_of_intermediate])
    # Because outputs might share a ._base, or an output's ._base might itself be
    # another user output (in both cases, we won't redundantly append bases to the end of the graph)
    num_intermediate_bases: int

    # For inference only: instructs us to keep data-only input mutations directly in the graph
    keep_input_mutations: bool

    # length = (# inputs w data mutations) + (# user outputs that are non_aliasing tensors)
    #        + (# intermediate bases)
    # These are the FakeTensor (or potential SymInt) outputs that we traced from our
    # metadata pass of the user's forward function.
    # Their only use today is to pass them as a best-guess for tangents when tracing the joint.
    # Stashing them as part of our "metadata" makes it simpler if we want to run our analysis
    # pass once, and re-use the output throughout AOTAutograd
    traced_tangents: List[Any]

    # Each of these is a list telling us about subclasses for the inputs/outputs/grad_outs
    # They are used throughout AOTDispatch to tell us how to generate a list of subclass tensors,
    # Given a (potentially larger) list of plain torch tensors.

    # Taking subclass_inp_meta as an example:
    #   subclass_inp_meta[i] = j (an int) tells us:
    #     "The i'th user input is not a subclass, and corresponds to inputs[j] of the plain-tensor graph."
    #   subclass_inp_meta[i] = SubclassCreationMeta(flat_tensor_start_idx=3, arg_count=2)
    #     "The i'th user input is subclass holding two inner tensors, which are
    #      inputs[3] and inputs[4] of the plain-tensor graph".

    # length = # user inputs
    subclass_inp_meta: List[Union[int, SubclassCreationMeta]]
    # So, the full set of outputs to the forward graph looks something like:
    # (*mutated_inps, *user_outs, *intermediate_bases, *saved_for_bw_tensors)
    # where the first 3 of those 4 can be subclasses
    # (but not saved_for_bw tensors, since these are internal to the compiler
    # and not user visible, so there's no point in wrapping/unwrapping them at runtime).
    # This list contains subclass information on all of the fw graph outputs
    # except for saved_for_bw_tensors.
    subclass_fw_graph_out_meta: List[Union[int, SubclassCreationMeta]]
    # length = # backward graph inputs
    subclass_tangent_meta: List[Union[int, SubclassCreationMeta]]
    # TODO: we should kill this
    # (need to default it to not break internal)
    is_train: bool = False
    # We're plumbing this requires_subclass_dispatch here is because it's painful to support input mutations
    # on subclasses, and that info isn't easily available.
    requires_subclass_dispatch: bool = False

    num_symints_saved_for_bw: Optional[int] = None

    # The grad_enabled mutation that will be emitted in the runtime_wrapper epilogue
    # NOTE: AOTAutograd will assume that the ambient `is_grad_enabled` is the grad mode
    # that is intended to be in effect prior to running the graph, in keeping with
    # equivalence to eager mode. It is the responsibility of upstream graph acquisition
    # to reset the grad mode to its pre-graph value prior to calling aot_autograd.
    grad_enabled_mutation: Optional[bool] = None

    def __post_init__(self):
        # pre-compute the indices of the inputs that are mutated.
        # When keep_input_mutations is set, we don't need to worry about our epilogue
        # handling data-only mutations, because we keep them directly in the graph.

        # TODO (tmanlaibaatar) Ideally input mutation type should be calculated
        # based on requires_subclass_dispatch argument but this is not easy to do because you would
        # have to pass around this argument multiple level down.
        if not self.requires_subclass_dispatch:
            mutated_inp_runtime_indices = [
                i
                for i, m in enumerate(self.input_info)
                if (m.mutation_type == MutationType.MUTATED_OUT_GRAPH)
            ]
        else:
            mutated_inp_runtime_indices = [
                i
                for i, m in enumerate(self.input_info)
                if m.mutation_type
                in (MutationType.MUTATED_IN_GRAPH, MutationType.MUTATED_OUT_GRAPH)
            ]

        mutated_graph_handled_indices = [
            i
            for i, m in enumerate(self.input_info)
            if m.mutation_type == MutationType.MUTATED_IN_GRAPH
            and not self.requires_subclass_dispatch
        ]
        self.mutated_graph_handled_indices = mutated_graph_handled_indices
        self.num_mutated_graph_handled_indices = len(self.mutated_graph_handled_indices)

        aliased_out_indices = [
            i
            for i, m in enumerate(self.output_info)
            if m.output_type
            not in [
                OutputType.non_alias,
                OutputType.unsafe_view_alias,
                OutputType.custom_function_view,
            ]
        ]
        unsafe_view_out_indices = [
            i
            for i, m in enumerate(self.output_info)
            if m.output_type is OutputType.unsafe_view_alias
        ]

        # This is pre-computed in post_init for perf.
        # It contains the index of every element
        # of input_info that corresponds to a mutation (data or metadata or both)
        self.mutated_inp_runtime_indices = mutated_inp_runtime_indices
        self.num_mutated_inp_runtime_indices = len(self.mutated_inp_runtime_indices)

        # This is pre-computed for perf.
        # It contains the index of every element
        # of output_info that corresponds to an alias (either of an input or intermediate)
        self.aliased_out_indices = aliased_out_indices
        self.unsafe_view_out_indices = unsafe_view_out_indices
        self.num_outputs = len(self.output_info)
        self.num_outputs_non_aliased = len(
            [
                x
                for x in self.output_info
                if x.output_type
                in [
                    OutputType.non_alias,
                    OutputType.unsafe_view_alias,
                    OutputType.custom_function_view,
                ]
            ]
        )
        self.num_outputs_aliased_to_inputs = len(
            [
                x
                for x in self.output_info
                if x.output_type
                in [
                    OutputType.alias_of_input,
                    OutputType.is_input,
                ]
            ]
        )
        self.num_unsafe_view_outputs = len(self.unsafe_view_out_indices)
        self.num_outputs_aliased_to_intermediates = len(
            [
                x
                for x in self.output_info
                if x.output_type
                in [
                    OutputType.alias_of_intermediate,
                    OutputType.alias_of_intermediate_save_as_output,
                    OutputType.alias_of_intermediate_base_is_user_output,
                ]
            ]
        )
        self.num_outputs_aliased = (
            self.num_outputs_aliased_to_inputs
            + self.num_outputs_aliased_to_intermediates
        )

        self.dynamic_outputs = any(o.dynamic_dims for o in self.output_info)
        # See Note: [AOTAutograd Backward Guards]
        # This is pre-computed for fast asserts on the types of our grad_outputs in the backward.
        # Eventually, we should kill this and replace with real backward guards.
        # (we want to precompute the "runtime" types, so replace FakeTensor with torch.Tensor)
        self.output_types = [
            torch.Tensor if isinstance(x, FakeTensor) else type(x)
            for x in self.traced_tangents
        ]

        self.is_rng_op_functionalized = config.functionalize_rng_ops
        # All of the above metadata is collected by tracing the fw function.
        # However, extra outputs for rng offsets behave differently. Both fwd
        # and bwd graphs have their own outputs for the total consumed offsets.
        # Unlike mutated inputs, we don't have to worry about sending the right
        # set of tensors between fwd and bwd. Fwd and bwd offsets are
        # independent and simpler to handle. Therefore, we track them
        # separately.
        self.num_outputs_rng_offset = 1 if self.is_rng_op_functionalized else 0

        # Our forward() returns both (mutated_inputs, outputs, output_intermediate_bases, saved_tensors, saved_symints)
        self.num_forward_returns = (
            self.num_mutated_inp_runtime_indices
            + self.num_outputs
            + self.num_intermediate_bases
        )
        # In case of functionalization of rng ops, the fw_module returns one
        # additional output for rng offset. This rng offset is used right
        # away to advance the rng state, and is not passed on to the raw
        # outputs. However, we need to know the exact boundary to identify
        # which tensors to be saved for the bwd graph.  num_forward captures
        # this information.
        self.num_forward = self.num_forward_returns + self.num_outputs_rng_offset

    @property
    def tensors_saved_for_backwards_slice(self):
        assert self.num_symints_saved_for_bw is not None
        if self.num_symints_saved_for_bw > 0:
            return slice(self.num_forward, -self.num_symints_saved_for_bw)
        else:
            return slice(self.num_forward, None)

    @property
    def symints_saved_for_backwards_slice(self):
        assert self.num_symints_saved_for_bw is not None
        if self.num_symints_saved_for_bw > 0:
            return slice(-self.num_symints_saved_for_bw, None)
        else:
            return slice(0, 0)  # empty slice

    def __eq__(self, other):
        if not isinstance(other, ViewAndMutationMeta):
            return NotImplemented
        return (
            self.input_info == other.input_info
            and self.output_info == other.output_info
            and self.num_intermediate_bases == other.num_intermediate_bases
            and self.keep_input_mutations == other.keep_input_mutations
            and self.is_rng_op_functionalized == other.is_rng_op_functionalized
            and self.num_outputs_rng_offset == other.num_outputs_rng_offset
            and len(self.traced_tangents) == len(other.traced_tangents)
            and all(
                x.shape == y.shape and x.dtype == y.dtype
                for x, y, in zip(self.traced_tangents, other.traced_tangents)
            )
        )


@dataclass(eq=False)
class SubclassMeta:
    # A copy of all forward metadata, but computed on the *dense* tensor forward (after desugaring subclasses)
    # So for example, if the user had a model containing two `TwoTensor` inputs,
    # Then `SubclassMeta.fw_metadata.input_infos` would have length 4 here.
    fw_metadata: ViewAndMutationMeta

    # Note: [Computing Subclass Metadata about grad_inputs]
    # Given a list of flattened, plain tensor grad_inputs, this tells us how to reconstruct the grad_input subclasses
    #
    # You might think: why not just assume that all grad_inputs will have the same subclass-ness as the original inputs?
    # (AOTAutograd generally assumes other properties, e.g. that grad_outputs are contiguous)
    #
    # This doesn't really work though. take this example:
    #
    # def f(DoubleTensor, DenseTensor):
    #     return DoubleTensor  * DenseTensor
    #
    # In the above example, the .grad field of *both* DoubleTensor and DenseTensor will be a DoubleTensor.
    # When we trace out a joint fw-bw graph, we'll end up returning two subclasses for the two grad_inputs.
    # This means that our backward graph will return 4 outputs (two dense tensors for each DoubleTensor grad_input)
    # and we need to properly store the metadata that tells us how to turn these 4 outputs back into DoubleTensors.
    #
    # Note that this info **cannot** easily be figured out from ViewAndMutationMeta.
    # We can only compute this info by tracing the entire joint and examining the grad_inputs that we computed.
    #
    # See Note: [AOTAutograd Backward Guards]
    # This will also eventually require us to install backward guards,
    # in case we made incorrect assumptions about the subclass-ness of our grad_outputs
    #
    # Optional field because we don't compute for inference graphs
    grad_input_metas: Optional[List[Union[int, SubclassCreationMeta]]]

    def __init__(self):
        # The fields in this class get set after its construction.
        pass


# This class exists because:
# - the autograd.Function.forward() in aot autograd returns outputs that might alias inputs
# - we only care about the metadata on those aliases, so we can regenerate them.
#   We do not want them to participate in the autograd.Function.
# We do that by wrapping them in an opaque class, so the autograd.Function
# does not know to treat them as tensors.
@dataclass(frozen=True)
class TensorAlias:
    alias: torch.Tensor
