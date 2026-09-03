variable "images" {
  description = "Explicit MicroVM images to build through AWS Cloud Control. Empty creates nothing."
  type = map(object({
    name                       = string
    description                = string
    code_artifact_uri          = string
    base_image_arn             = string
    base_image_version         = string
    build_role_arn             = string
    minimum_memory_mib         = number
    egress_network_connectors  = optional(set(string), [])
    additional_os_capabilities = optional(set(string), [])
    environment_variables      = optional(map(string), {})
    hooks = optional(object({
      port = optional(number)
      microvm_hooks = optional(object({
        run                          = optional(string)
        run_timeout_in_seconds       = optional(number)
        resume                       = optional(string)
        resume_timeout_in_seconds    = optional(number)
        suspend                      = optional(string)
        suspend_timeout_in_seconds   = optional(number)
        terminate                    = optional(string)
        terminate_timeout_in_seconds = optional(number)
      }))
      microvm_image_hooks = optional(object({
        ready                       = optional(string)
        ready_timeout_in_seconds    = optional(number)
        validate                    = optional(string)
        validate_timeout_in_seconds = optional(number)
      }))
    }), {})
    logging = optional(object({
      disabled = optional(bool)
      cloudwatch = optional(object({
        log_group  = optional(string)
        log_stream = optional(string)
      }))
    }), { disabled = true })
    tags = optional(map(string), {})
  }))
  default = {}

  validation {
    condition = alltrue([
      for image in values(var.images) :
      contains([512, 1024, 2048, 4096, 8192], image.minimum_memory_mib) &&
      alltrue([for capability in image.additional_os_capabilities : capability == "ALL"])
    ])
    error_message = "minimum_memory_mib must be one of 512, 1024, 2048, 4096, or 8192; ALL is the only supported elevated OS capability."
  }
}

variable "tags" {
  description = "Tags merged into each explicitly managed image."
  type        = map(string)
  default     = {}
}
