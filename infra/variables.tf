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

variable "trusted_proxy_hops" {
  description = "How many proxies in front of the service are ours. 1 for Cloud Run alone; 2 once Firebase Hosting or a load balancer is in front of it. Wrong in either direction breaks a security control - see the validation below and ratelimit.py."
  type        = number
  default     = 1

  validation {
    # Not a free-form number. `X-Forwarded-For` is appended to by each hop, so `client_ip()` takes
    # the Nth entry from the right and only the rightmost N were added by infrastructure we own.
    #
    # Too low and every visitor resolves to the same address - the CDN's edge, or Cloud Run's own
    # front end - so they all share one rate-limit bucket and `subscribe_limit_per_hour` becomes a
    # global cap of five signups an hour for the whole service. That failure looks exactly like the
    # unexplained 422s of 2026-09.
    #
    # Too high and `len(parts) >= hops` fails, which falls back to the socket peer: same shared
    # bucket. And at a value the header *can* reach, the client is picking its own identity out of
    # a header it wrote, which is F-5 in SECURITY_REVIEW.md - every limit becomes decorative.
    condition     = var.trusted_proxy_hops >= 1 && var.trusted_proxy_hops <= 3
    error_message = "trusted_proxy_hops counts real proxies: 1 for Cloud Run alone, 2 behind Firebase Hosting or a load balancer. 0 would take the socket peer, which behind Cloud Run is one address for every visitor."
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

variable "vapid_subject" {
  description = "The contact RFC 8292 puts in the VAPID `sub` claim. Google, Apple and Mozilla receive it on every send and use it to reach the operator when something is wrong with our traffic - the same role dwd_user_agent plays for DWD. A role address, not a personal one. Must be mailto: or https:."
  type        = string
  default     = ""

  validation {
    # A malformed subject is accepted by some push services and rejected by others, which is the
    # worst outcome: it works in testing and fails for a subset of subscribers. Empty is allowed
    # so that a deployment can fall back to alert_email below.
    condition     = var.vapid_subject == "" || startswith(var.vapid_subject, "mailto:") || startswith(var.vapid_subject, "https://")
    error_message = "vapid_subject must start with mailto: or https://, or be empty to derive it from alert_email."
  }
}

variable "map_tile_url" {
  description = "Basemap tile template. Defaults to basemap.de Web Raster (BKG): CC BY 4.0, no key, no quota, no non-commercial clause, Germany only - which matches the radar's own footprint (DESIGN.md D-43). Note it is WMTS, so the order is {z}/{y}/{x}, y before x. Set to \"\" along with map_tile_attribution for no basemap at all: the map then draws the radar over a graticule with cities marked, which is a supported state rather than a broken one."
  type        = string
  default     = "https://sgx.geodatenzentrum.de/wmts_basemapde/tile/1.0.0/de_basemapde_web_raster_farbe/default/GLOBAL_WEBMERCATOR/{z}/{y}/{x}.png"

  validation {
    # Deliberately does NOT check the {z}/{y}/{x} vs {z}/{x}/{y} order: both are legitimate
    # depending on the provider, so Terraform cannot know which is right. What it does catch is
    # pasting a finished tile URL with the numbers already substituted in, which is the one
    # version of this mistake that is unambiguously wrong. The order is checked instead against
    # the shipped default, in tests/test_vendored_leaflet.py.
    condition     = var.map_tile_url == "" || (strcontains(var.map_tile_url, "{z}") && strcontains(var.map_tile_url, "{x}") && strcontains(var.map_tile_url, "{y}"))
    error_message = "map_tile_url must contain the {z}, {x} and {y} placeholders, or be empty for no basemap."
  }
}

variable "vector_tile_url" {
  description = "Shortbread vector tiles for the maps (DESIGN.md D-58, D-59). A variable, so switching provider - or turning the vector map off with \"\" - is a tfvars change and an apply, not a new image: the OSMF's vector tile policy recommends exactly that, because it may block a user without notice. Empty draws both maps with Leaflet and map_tile_url."
  type        = string
  default     = "https://vector.openstreetmap.org/shortbread_v1/{z}/{x}/{y}.mvt"

  validation {
    condition     = var.vector_tile_url == "" || (startswith(var.vector_tile_url, "https://") && strcontains(var.vector_tile_url, "{z}") && strcontains(var.vector_tile_url, "{x}") && strcontains(var.vector_tile_url, "{y}"))
    error_message = "vector_tile_url must be an https:// tile template with {z}, {x} and {y}, or empty to turn the vector map off."
  }
}

variable "contact_email" {
  description = "Shown in every page's footer when set. The OSMF's vector tile policy recommends a contact on the site - without one, their only option when something goes wrong is to block it. It is published to everyone who opens the site, so use an address meant for that. Empty shows none."
  type        = string
  default     = ""

  validation {
    condition     = var.contact_email == "" || can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.contact_email))
    error_message = "contact_email must be an email address, or empty."
  }
}

variable "device_key_login_enabled" {
  description = "Push subscribers' browsers sign their settings requests with a device key, so the settings open without a push round trip (DESIGN.md D-64). The kill switch: false falls back to the push link and the cookie session for everyone, and nothing is lost. If it was turned off because of a verification bug, empty device_keys before turning it back on (RUNBOOK)."
  type        = bool
  default     = true
}

variable "transactional_mail_cap_per_day" {
  description = "Confirmation and settings-link mails per rolling 24 h, across all requests (DESIGN.md D-63). The only limit on them a forged client IP cannot get around. Keep it plus the warnings' own cap within the mail provider's daily quota. 0 disables it."
  type        = number
  default     = 50

  validation {
    condition     = var.transactional_mail_cap_per_day >= 0
    error_message = "transactional_mail_cap_per_day must be 0 (off) or a positive number."
  }
}

variable "map_tile_attribution" {
  description = "Required by every provider worth using, and by their licence. Shown in the map's corner. Change it whenever you change map_tile_url - an attribution that credits the wrong service is worse than none."
  type        = string
  default     = "&copy; <a href=\"https://www.bkg.bund.de\">BKG</a> (basemap.de) <a href=\"https://creativecommons.org/licenses/by/4.0/\">CC BY 4.0</a>"

  validation {
    # The one pairing that is actually wrong: tiles from somebody with no credit on screen.
    condition     = var.map_tile_url == "" || var.map_tile_attribution != ""
    error_message = "map_tile_attribution cannot be empty when map_tile_url is set: every provider requires credit, and so does their licence."
  }
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

  # Validated because `vapid_subject` falls back to "mailto:${alert_email}", and an empty value
  # produced the subject "mailto:" - which passes that variable's own check and `WebPushNotifier`'s
  # `startswith`, and is then sent to Google, Apple and Mozilla in the VAPID claim of every push we
  # make. A push service is entitled to reject a `sub` it cannot contact, so the failure mode is
  # every notification silently refused, configured by omission.
  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be a real address: vapid_subject falls back to a mailto: of it, and that is sent to every push service."
  }
}
