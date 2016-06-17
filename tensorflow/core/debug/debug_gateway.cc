/* Copyright 2016 The TensorFlow Authors. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
==============================================================================*/

#include "tensorflow/core/debug/debug_gateway.h"

#include "tensorflow/core/common_runtime/device_factory.h"
#include "tensorflow/core/common_runtime/session_factory.h"
#include "tensorflow/core/framework/tensor.h"

namespace tensorflow {

DebugGateway::DebugGateway(DirectSession* session)
    : session_(session) {
  session_->node_output_callback_ = [this](const string& node_name,
                                           const int output_slot,
                                           const Tensor* tensor,
                                           const bool is_ref,
                                           OpKernelContext* ctx) {
    if (comp_cb_ != nullptr) {
      comp_cb_(node_name, output_slot, is_ref);
    }

    // Copy tensor values (e.g., from GPU to host) only if the
    // value callback is not nullptr.
    if (val_cb_ != nullptr) {
      CopyTensor(
          node_name, output_slot, tensor, ctx,
          [this, node_name, output_slot, is_ref](const Tensor* copied_tensor) {
            val_cb_(node_name, output_slot, *copied_tensor, is_ref);
          });
    }

    return Status::OK();
  };
}

DebugGateway::~DebugGateway() {
  ClearHostTensors();

  if (session_ != nullptr) {
   session_->node_output_callback_ = nullptr;
  }
}

void DebugGateway::SetNodeCompletionCallback(NodeCompletionCallback callback) {
  comp_cb_ = callback;
}

void DebugGateway::SetNodeValueCallback(NodeValueCallback callback) {
  val_cb_ = callback;
}

void DebugGateway::CopyTensor(const string& node_name, const int output_slot,
                              const Tensor* src_tensor, OpKernelContext* ctx,
                              CopyDoneCallback copy_done_cb) {
  Device* device = static_cast<Device*>(ctx->device());

  // Create copied tensor on host
  AllocatorAttributes alloc_attrs = ctx->output_alloc_attr(output_slot);
  Allocator* cpu_allocator = tensorflow::cpu_allocator();
  Tensor* cpu_tensor =
      new Tensor(cpu_allocator, src_tensor->dtype(), src_tensor->shape());

  // Keep track of the tensors created for copying so that can be freed
  // later.
  {
    mutex_lock l(mu_);
    host_tensors_[node_name] = cpu_tensor;
  }

  // Determine if the tensor is initialized properly.
  if (src_tensor->IsInitialized() && DataTypeSize(src_tensor->dtype()) > 0) {
    // Tensor is initialized: Make copy.

    // Determine if the tensor is on device (GPU) or host (CPU).
    if (device->name().find("gpu:") != string::npos &&
        !alloc_attrs.on_host()) {
      // GPU tensors: Copy it to host (CPU).
      DeviceContext* device_ctxt = ctx->op_device_context();

      // Copy device (e.g., GPU) tensor to host and when done, invoke the
      // callback.
      device_ctxt->CopyDeviceTensorToCPU(
          src_tensor, "TensorCopy", device, cpu_tensor,
          [node_name, cpu_tensor, copy_done_cb](const Status& s) {
            if (s.ok()) {
              copy_done_cb(cpu_tensor);
            } else {
              LOG(ERROR) << "Copying of device Tensor " << node_name
                         << " to CPU for debugging failed.";
            }
          });
    } else {
      // For CPU tensors, copy the source tensor and own the copy, because the
      // value callback may outlive the life time of the source tensor.
      cpu_tensor->UnsafeCopyFromInternal(*src_tensor, src_tensor->shape());

      copy_done_cb(cpu_tensor);
    }
  } else {
    // Tensor is not initialized: No need to copy.
    copy_done_cb(cpu_tensor);
  }
}

void DebugGateway::ClearHostTensors() {
  mutex_lock l(mu_);
  for (auto it = host_tensors_.begin(); it != host_tensors_.end(); ++it) {
    if (it->second != nullptr) {
      delete it->second;
    }
  }

  host_tensors_.clear();
}

}  // namespace tensorflow
