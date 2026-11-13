## ---------------------------------------------------------------------------
## Data cleaning: quarantine of defective records and detection-history build.
##
## Quarantine rules (a record is never silently repaired):
##   duplicate_site_key           same site_id with conflicting attributes
##   conflicting_detection        same (site_id, visit_id), contradictory records
##   date_inversion               visit_date earlier than an earlier-indexed visit
##   detection_without_opportunity detection = 1 on a cancelled / failed visit
##   missing_detection_value      opportunity visit without a 0/1 detection value
##
## Detection opportunity: completed & device_ok.  Valid detection = 1,
## valid non-detection = 0, anything without an opportunity is NA (never 0).
## ---------------------------------------------------------------------------

.quar_bind <- function(df, reason) {
  if (nrow(df) == 0) return(NULL)
  df$reason <- reason
  df
}

quarantine_sites <- function(sites) {
  keys <- unique(sites$site_id[duplicated(sites$site_id)])
  conflict_keys <- character(0)
  for (k in keys) {
    rows <- sites[sites$site_id == k, , drop = FALSE]
    if (nrow(unique(rows)) > 1L) conflict_keys <- c(conflict_keys, k)
  }
  quarantine <- .quar_bind(sites[sites$site_id %in% conflict_keys, ],
                           "duplicate_site_key")
  clean <- sites[!sites$site_id %in% conflict_keys, ]
  clean <- clean[!duplicated(clean$site_id), ]   # exact duplicates: keep one
  rownames(clean) <- NULL
  list(clean = clean,
       quarantine = if (is.null(quarantine)) empty_quar_sites() else quarantine)
}

empty_quar_sites <- function() {
  data.frame(site_id = character(0), reason = character(0),
             stringsAsFactors = FALSE)
}

quarantine_visits <- function(visits) {
  v <- visits
  v$.row <- seq_len(nrow(v))
  reason <- vector("list", nrow(v))

  ## (a) conflicting records for the same (site_id, visit_id)
  key <- paste(v$site_id, v$visit_id, sep = "\r")
  for (k in unique(key[duplicated(key)])) {
    idx <- which(key == k)
    sig <- unique(v[idx, c("completed", "device_ok", "detection")])
    if (nrow(sig) > 1L) {
      for (i in idx) reason[[i]] <- c(reason[[i]], "conflicting_detection")
    } else {
      ## exact duplicates: keep the first, quarantine the copies
      for (i in idx[-1]) reason[[i]] <- c(reason[[i]], "exact_duplicate")
    }
  }

  ## (b) visit dates must be non-decreasing in visit_id within a site
  ord <- order(v$site_id, v$visit_id)
  for (s in unique(v$site_id)) {
    idx <- ord[v$site_id[ord] == s]
    d   <- as.numeric(v$visit_date[idx])
    bad <- d < cummax(d)
    for (i in idx[bad]) reason[[i]] <- c(reason[[i]], "date_inversion")
  }

  ## (c) detection recorded without a detection opportunity
  no_opp  <- !v$completed | !v$device_ok
  has_det <- !is.na(v$detection) & v$detection == 1L
  for (i in which(no_opp & has_det))
    reason[[i]] <- c(reason[[i]], "detection_without_opportunity")

  ## (d) opportunity visit missing its 0/1 detection value
  opp <- v$completed & v$device_ok
  for (i in which(opp & is.na(v$detection)))
    reason[[i]] <- c(reason[[i]], "missing_detection_value")

  is_bad <- lengths(reason) > 0
  quarantine <- v[is_bad, , drop = FALSE]
  quarantine$reason <- vapply(reason[is_bad], paste, collapse = ";", FUN.VALUE = "")
  clean <- v[!is_bad, , drop = FALSE]
  clean$.row <- NULL
  quarantine$.row <- NULL
  rownames(clean) <- NULL
  list(clean = clean, quarantine = quarantine)
}

## Detection-history matrix (sites x visits) plus per-visit observer/device
## configuration matrices.  Sites with zero detection opportunities carry no
## information and are dropped (recorded in $dropped_sites).
build_history <- function(visits_clean, sites_pinned, J = 4) {
  keep_ids <- sort(unique(sites_pinned$site_id))
  M <- length(keep_ids)
  dn <- list(keep_ids, paste0("v", seq_len(J)))
  y   <- matrix(NA_integer_,   M, J, dimnames = dn)
  obs <- matrix(NA_character_, M, J, dimnames = dn)
  dev <- matrix(NA_character_, M, J, dimnames = dn)
  opp <- matrix(FALSE,         M, J, dimnames = dn)
  for (k in seq_len(nrow(visits_clean))) {
    r <- visits_clean[k, ]
    if (!r$site_id %in% keep_ids || r$visit_id > J) next
    opportunity <- isTRUE(r$completed) && isTRUE(r$device_ok)
    opp[r$site_id, r$visit_id] <- opportunity
    if (opportunity) {
      y[r$site_id, r$visit_id]   <- as.integer(r$detection)
      obs[r$site_id, r$visit_id] <- r$observer_id
      dev[r$site_id, r$visit_id] <- r$device_id
    }
  }
  no_opp  <- rowSums(opp) == 0
  dropped <- rownames(y)[no_opp]
  list(y = y[!no_opp, , drop = FALSE],
       observer = obs[!no_opp, , drop = FALSE],
       device   = dev[!no_opp, , drop = FALSE],
       opportunity = opp[!no_opp, , drop = FALSE],
       dropped_sites = dropped)
}
