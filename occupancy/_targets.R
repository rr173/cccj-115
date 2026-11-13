## ---------------------------------------------------------------------------
## targets pipeline: synthetic survey -> quarantine -> versioned covariates ->
## detection history -> single-season occupancy fit -> gated publication.
##
## Re-running the pipeline reuses cached targets when inputs are unchanged;
## revising inputs (cov_version, formulas, data) creates a new analysis_id
## and therefore a new versioned batch under output/.
## ---------------------------------------------------------------------------

library(targets)

for (f in list.files("R", pattern = "\\.R$", full.names = TRUE)) source(f)

tar_option_set(
  packages = c("unmarked", "digest", "MASS"),
  seed = 20241113
)

## Model formulas as stable strings (hashed into the analysis identity).
STATE_RHS <- "elev + forest"
DET_RHS   <- "obs_mid + obs_low + dev_aru"

list(
  ## ---- synthetic raw data -------------------------------------------------
  tar_target(sim, sim_survey(n_sites = 120, J = 4, seed = 20241113)),

  ## ---- cleaning / quarantine ----------------------------------------------
  tar_target(sites_q,  quarantine_sites(sim$sites)),
  tar_target(visits_q, quarantine_visits(sim$visits)),

  ## ---- covariate version pinned per analysis ------------------------------
  tar_target(cov_version, "v1"),
  tar_target(sites_pinned, pin_covariates(sites_q$clean, cov_version)),

  ## ---- detection history (NA = no opportunity, never imputed 0) -----------
  tar_target(hist, build_history(visits_q$clean, sites_pinned, J = 4)),
  tar_target(umf,  make_umf(hist, sites_pinned)),

  ## ---- stable identities ---------------------------------------------------
  tar_target(state_rhs, STATE_RHS),
  tar_target(det_rhs,   DET_RHS),
  tar_target(ssid, site_set_id(rownames(hist$y))),
  tar_target(fid,  formula_id(state_rhs, det_rhs)),
  tar_target(aid,  analysis_id(ssid, fid, cov_version)),

  ## ---- fit + diagnostics + publication gate --------------------------------
  tar_target(fit,  fit_occupancy(umf, state_rhs, det_rhs, seed = 20241113)),
  tar_target(diag, diagnose_fit(fit)),
  tar_target(result, publish_or_withhold(fit, diag, aid)),
  tar_target(preds,     predict_state(fit, umf, diag)),
  tar_target(vcov_mat,  get_vcov(fit, diag)),
  tar_target(crosscheck,
             likelihood_crosscheck(fit, umf, state_rhs, det_rhs, diag)),

  ## ---- versioned batch output + manifest ------------------------------------
  tar_target(batch_dir,
             save_batch("output", aid, ssid, fid, cov_version,
                        hist, sites_pinned, result, preds, vcov_mat,
                        crosscheck)),
  tar_target(manifest,
             update_manifest(file.path("output", "manifest.csv"),
                             aid, ssid, fid, cov_version, result$status))
)
