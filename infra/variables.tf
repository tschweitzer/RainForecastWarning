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
  description = "Origin every link in every message is built from, and the overlays bucket's allowed CORS origin. Two-pass by nature: the service has to exist before its URL is known, and it cannot reference its own uri without a dependency cycle. Leave it empty for the first apply, then set it to the api_url output and apply again."
  type        = string
  default     = ""

  validation {
    # The example file ships a placeholder with an obvious hole in it. Pasting that unedited
    # configures a service whose every link points at a host that does not exist and whose
    # overlay bucket allows an origin nobody browses from - and nothing downstream complains,
    # because it is a perfectly well-formed URL.
    condition     = var.public_base_url == "" || can(regex("^https://[^X]+$", var.public_base_url))
    error_message = "public_base_url still contains the XXXXXXXX placeholder. Leave it empty for the first apply, then set it to `terraform output api_url`."
  }

  validation {
    condition     = var.public_base_url == "" || startswith(var.public_base_url, "https://")
    error_message = "public_base_url must be https:// - the session cookie's Secure flag and the browser geolocation API are both decided by this string."
  }
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

variable "map_tile_url" {
  description = "Basemap tile template, e.g. https://tiles.example.com/{z}/{x}/{y}.png?key=... Empty by default and deliberately so (DESIGN.md §2): the obvious choice, tile.openstreetmap.org, is volunteer-run, its usage policy excludes applications, and it blocks them. With nothing set the map draws the radar over a graticule and a few cities, which is enough to read a rain field and costs nobody anything."
  type        = string
  default     = ""
}

variable "map_tile_attribution" {
  description = "Required by every provider worth using, and by their licence. Shown in the map's corner."
  type        = string
  default     = ""
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
