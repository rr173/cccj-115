## ---------------------------------------------------------------------------
## Covariate versioning and stable analysis identities.
##
## * pin_covariates() freezes site covariates to one release (v1 / v2) for the
##   whole analysis; the (zone, season) strata all inherit that version tag.
## * site_set_id / formula_id / analysis_id are content hashes: re-running the
##   same analysis reproduces the same id (reuse), while any input revision or
##   formula change yields a new id (new version).
## ---------------------------------------------------------------------------

pin_covariates <- function(sites, cov_version) {
  stopifnot(cov_version %in% c("v1", "v2"))
  out <- data.frame(
    site_id = sites$site_id,
    zone    = sites$zone,
    season  = sites$season,
    elev    = sites[[paste0("elev_",   cov_version)]],
    forest  = sites[[paste0("forest_", cov_version)]],
    cov_version = cov_version,
    stringsAsFactors = FALSE
  )
  rownames(out) <- NULL
  out
}

site_set_id <- function(site_ids) {
  digest::digest(paste(sort(unique(site_ids)), collapse = "|"), algo = "sha1")
}

formula_id <- function(state_rhs, det_rhs) {
  digest::digest(paste(state_rhs, det_rhs, sep = " ~ "), algo = "sha1")
}

analysis_id <- function(site_set_id, formula_id, cov_version) {
  h <- digest::digest(paste(site_set_id, formula_id, cov_version, sep = "|"),
                      algo = "sha1")
  paste0("occ-", substr(h, 1, 12))
}

## Append-or-reuse manifest: one row per analysis version.
update_manifest <- function(manifest_path, aid, ssid, fid, cov_version,
                            status, note = "") {
  row <- data.frame(
    analysis_id = aid, site_set_id = ssid, formula_id = fid,
    cov_version = cov_version, status = status, note = note,
    created_utc = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"),
    stringsAsFactors = FALSE
  )
  if (file.exists(manifest_path)) {
    old <- utils::read.csv(manifest_path, stringsAsFactors = FALSE)
    old[is.na(old)] <- ""
    if (aid %in% old$analysis_id) {
      return(list(action = "reused", manifest = old))
    }
    manifest <- rbind(old, row)
  } else {
    manifest <- row
  }
  dir.create(dirname(manifest_path), recursive = TRUE, showWarnings = FALSE)
  utils::write.csv(manifest, manifest_path, row.names = FALSE)
  list(action = "new_version", manifest = manifest)
}
