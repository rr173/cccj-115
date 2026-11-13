## ---------------------------------------------------------------------------
## Single-season occupancy model (MacKenzie et al. 2002) via unmarked::occu.
##
##   state  : latent occupancy probability psi  ~ site covariates
##   detect : conditional detection probability p ~ observer + device config
##
## Publishing gate: estimates are released only when the optimiser converged
## AND all parameters are identifiable (finite, sane standard errors; vcov
## well conditioned; no boundary estimates).  Otherwise the batch returns
## diagnostics that localise the problem instead of estimates.
## ---------------------------------------------------------------------------

make_umf <- function(hist, sites_pinned) {
  sc <- sites_pinned[match(rownames(hist$y), sites_pinned$site_id), ]
  stopifnot(all(sc$site_id == rownames(hist$y)))
  dummy <- function(mat, level)
    matrix(as.integer(mat == level), nrow = nrow(mat), dimnames = dimnames(mat))
  unmarked::unmarkedFrameOccu(
    y = hist$y,
    siteCovs = data.frame(elev = as.numeric(scale(sc$elev)),
                          forest = sc$forest,
                          row.names = sc$site_id),
    obsCovs = list(
      obs_mid = dummy(hist$observer, "OBS_B"),   # reference: OBS_A (senior)
      obs_low = dummy(hist$observer, "OBS_C"),
      dev_aru = dummy(hist$device,   "ARU")      # reference: CAM
    )
  )
}

fit_occupancy <- function(umf, state_rhs, det_rhs, seed = 123) {
  set.seed(seed)
  f <- stats::as.formula(paste("~", det_rhs, "~", state_rhs))
  tryCatch(
    unmarked::occu(f, data = umf),
    error = function(e) e
  )
}

## Diagnostics that localise *why* a fit cannot be published.
diagnose_fit <- function(fit, se_limit = 1e3, cond_limit = 1e10,
                         boundary_limit = 10) {
  if (inherits(fit, "error")) {
    return(list(ok = FALSE, stage = "optimisation",
                message = paste("fit error:", conditionMessage(fit))))
  }
  cf  <- coef(fit)          # unmarked S4 generic: do not use stats:: prefix
  V   <- tryCatch(vcov(fit), error = function(e) NULL)
  se  <- if (is.null(V)) rep(NA_real_, length(cf)) else suppressWarnings(sqrt(diag(V)))
  conv <- fit@opt$convergence
  na_se     <- names(cf)[!is.finite(se)]
  huge_se   <- names(cf)[is.finite(se) & se > se_limit]
  boundary  <- names(cf)[is.finite(cf) & abs(cf) > boundary_limit]
  cond_vcov <- tryCatch(if (is.null(V)) Inf else kappa(V), error = function(e) Inf)
  problems <- c(
    if (conv != 0)           sprintf("optimizer convergence code = %d", conv),
    if (length(na_se))       paste("non-finite SE:", paste(na_se, collapse = ", ")),
    if (length(huge_se))     paste("SE above limit:", paste(huge_se, collapse = ", ")),
    if (length(boundary))    paste("boundary estimate |beta| >", boundary_limit, ":",
                                   paste(boundary, collapse = ", ")),
    if (!is.finite(cond_vcov) || cond_vcov > cond_limit)
      sprintf("vcov condition number %.3g above limit", cond_vcov)
  )
  list(ok = length(problems) == 0,
       stage = if (length(problems)) "diagnostics" else "ok",
       convergence_code = conv,
       cond_vcov = cond_vcov,
       se = se, n_params = length(cf),
       logLik = -fit@opt$value,
       problems = problems,
       message = if (length(problems)) paste(problems, collapse = " | ") else "ok")
}

## Gate: publish estimates + covariance, or withhold and return diagnostics.
publish_or_withhold <- function(fit, diag, aid) {
  if (!diag$ok) {
    return(list(status = "withheld", analysis_id = aid, diagnostics = diag))
  }
  cf <- coef(fit)
  se <- diag$se
  z  <- stats::qnorm(0.975)
  est <- data.frame(
    parameter = names(cf), estimate = cf, se = se,
    lwr = cf - z * se, upr = cf + z * se,
    row.names = NULL, stringsAsFactors = FALSE
  )
  list(status = "published", analysis_id = aid,
       estimates = est, vcov = vcov(fit),
       logLik = diag$logLik, diagnostics = diag)
}

## Per-site occupancy predictions with intervals.
## Wald interval on the logit scale (unmarked::predict) plus a parametric
## bootstrap interval drawn from the fitted parameter covariance.
predict_state <- function(fit, umf, diag, R = 1000, seed = 42) {
  if (!diag$ok) return(NULL)
  nd <- unmarked::siteCovs(umf)
  wald <- as.data.frame(unmarked::predict(fit, type = "state", newdata = nd))
  names(wald) <- c("psi", "se", "wald_lwr", "wald_upr")

  cf <- coef(fit)
  n_state <- length(coef(fit, type = "state"))
  V <- vcov(fit)[seq_len(n_state), seq_len(n_state), drop = FALSE]
  X <- stats::model.matrix(~ elev + forest, nd)
  set.seed(seed)
  draws <- MASS::mvrnorm(R, mu = cf[seq_len(n_state)], Sigma = V)
  sims <- plogis(X %*% t(draws))
  boot <- t(apply(sims, 1, stats::quantile, probs = c(0.025, 0.975),
                  names = FALSE))
  out <- data.frame(
    site_id = rownames(unmarked::getY(umf)),
    psi = wald$psi, se = wald$se,
    wald_lwr = wald$wald_lwr, wald_upr = wald$wald_upr,
    boot_lwr = boot[, 1], boot_upr = boot[, 2],
    row.names = NULL
  )
  stopifnot(all(out$psi >= 0 & out$psi <= 1),
            all(out$wald_lwr >= 0 & out$wald_upr <= 1),
            all(out$wald_lwr <= out$psi & out$psi <= out$wald_upr))
  out
}

get_vcov <- function(fit, diag) {
  if (!diag$ok) return(NULL)
  vcov(fit)
}
