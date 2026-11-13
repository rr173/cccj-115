## ---------------------------------------------------------------------------
## Batch output: one directory per analysis version under output/, keeping
## the detection history, covariate version, convergence info, parameter
## covariance and prediction intervals.  Withheld batches write diagnostics
## instead of estimates.
## ---------------------------------------------------------------------------

save_batch <- function(out_root, aid, ssid, fid, cov_version,
                       hist, sites_pinned, result, preds, vcov_mat,
                       crosscheck) {
  dir <- file.path(out_root, aid)
  dir.create(dir, recursive = TRUE, showWarnings = FALSE)

  utils::write.csv(data.frame(site_id = rownames(hist$y), hist$y,
                              check.names = FALSE),
                   file.path(dir, "detection_history.csv"), row.names = FALSE)
  utils::write.csv(sites_pinned, file.path(dir, "site_covariates.csv"),
                   row.names = FALSE)

  diag <- result$diagnostics
  conv <- data.frame(
    analysis_id = aid, site_set_id = ssid, formula_id = fid,
    cov_version = cov_version, status = result$status,
    convergence_code = if (is.null(diag$convergence_code)) NA else diag$convergence_code,
    cond_vcov = if (is.null(diag$cond_vcov)) NA else diag$cond_vcov,
    logLik = if (is.null(diag$logLik)) NA else diag$logLik,
    message = diag$message,
    stringsAsFactors = FALSE
  )
  utils::write.csv(conv, file.path(dir, "convergence.csv"), row.names = FALSE)

  if (result$status == "published") {
    utils::write.csv(result$estimates, file.path(dir, "estimates.csv"),
                     row.names = FALSE)
    utils::write.csv(vcov_mat, file.path(dir, "vcov.csv"))
    utils::write.csv(preds, file.path(dir, "predictions.csv"),
                     row.names = FALSE)
    if (!is.null(crosscheck))
      utils::write.csv(data.frame(hand_nll = crosscheck$hand_nll,
                                  pkg_nll  = crosscheck$pkg_nll,
                                  abs_diff = crosscheck$abs_diff,
                                  passed   = crosscheck$passed),
                       file.path(dir, "likelihood_crosscheck.csv"),
                       row.names = FALSE)
  } else {
    writeLines(c("ESTIMATES WITHHELD - fit did not pass diagnostics", "",
                 diag$problems),
               file.path(dir, "WITHHELD.txt"))
  }
  dir
}
