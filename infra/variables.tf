variable "project_id" {
  description = "GCP project."
  type        = string
  default     = "rainchecker-195519"
}

variable "region" {
  description = "Everything lives in one region: Cloud Run next to Cloud SQL keeps the database on a unix socket rather than the internet, and europe-west3 (Frankfurt) keeps German subscribers' data in Germany."
  type        = string
  default     = "europe-west3"
}

variable "public_base_url" {
  description = "Origin every emailed link is built from. Until a domain exists, set this to the Cloud Run service URL after the first apply and re-apply."
  type        = string
}

variable "mail_from" {
  description = "Sender address. Its domain needs SPF, DKIM and DMARC or the warnings land in spam. Unused while smtp_host is empty."
  type        = string
  default     = "RainAlert <noreply@invalid>"
}

variable "smtp_host" {
  description = "Any provider - they all speak SMTP. Leave empty for a push-only deployment: the signup page then offers push alone, the API refuses the email channel, and the SMTP password secret is not mounted. Setting it later turns email on."
  type        = string
  default     = ""
}

variable "smtp_port" {
  type    = number
  default = 587
}

variable "smtp_username" {
  type    = string
  default = ""
}

variable "ntfy_server" {
  description = "Where push notifications are published. The public server sees the topic name and the message text, and a rain warning names a time and an intensity - self-host it for anything past testing (DESIGN.md D-30)."
  type        = string
  default     = "https://ntfy.sh"
}

variable "image" {
  description = "Container image, by digest. A tag is mutable; a digest is what makes a rollback mean something."
  type        = string
}

variable "api_max_instances" {
  description = "A security control, not a tuning knob (SECURITY_REVIEW.md F-6). Unauthenticated traffic to any route that touches the database scales the service out until the database's connection ceiling is gone - starving the ingest job of the connection it needs to warn anyone, and billing us for it."
  type        = number
  default     = 4
}

variable "alert_email" {
  description = "Where operator alerts go. Not a subscriber address."
  type        = string
}
