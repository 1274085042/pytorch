#include <ATen/ATen.h>
#include <torch/library.h>
// TODO: Think about file name. Should it be shuffle.cpp or something else?

#ifdef USE_CUDA
void fsdpAllGatherCopyOut(
    std::vector<at::Tensor> params,
    at::Tensor allGatherRes,
    int64_t worldSize);
void unflatten_cat_with_pad_cuda(
    std::vector<at::Tensor> tensors,
    int64_t dim,
    int64_t factor,
    at::Tensor out
);
#endif

namespace {

void fsdp_all_gather_copy_out(
    std::vector<at::Tensor> params,
    at::Tensor all_gather_res,
    int64_t world_size) {
#ifdef USE_CUDA
  return fsdpAllGatherCopyOut(params, all_gather_res, world_size);
#else
  C10_THROW_ERROR(NotImplementedError, "Not implemented for CPU");
#endif
}

void unflatten_cat_with_pad(
  std::vector<at::Tensor> tensors,
  int64_t dim,
  int64_t factor,
  at::Tensor out
) {
#ifdef USE_CUDA
  return unflatten_cat_with_pad_cuda(tensors, dim, factor, out);
#else
  C10_THROW_ERROR(NotImplementedError, "Not implemented for CPU");
#endif
}

} // namespace

TORCH_LIBRARY_FRAGMENT(c10d, m) {
  m.def(
      "fsdp_all_gather_copy_out("
      "Tensor[] params, Tensor all_gather_res, int world_size) -> ()",
      torch::dispatch(
          c10::DispatchKey::CompositeExplicitAutograd,
          ::fsdp_all_gather_copy_out),
      {at::Tag::pt2_compliant_tag});
  m.def(
    "unflatten_cat_with_pad("
    "Tensor[] tensors, int dim, int factor, Tensor out) -> ()",
    torch::dispatch(
      c10::DispatchKey::CompositeExplicitAutograd,
      ::unflatten_cat_with_pad),
      {at::Tag::pt2_compliant_tag});
}
