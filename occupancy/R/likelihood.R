## ---------------------------------------------------------------------------
## Hand-written single-season occupancy likelihood, used as an independent
## cross-check of unmarked's fitted likelihood.
##
## For one site with detection history y (NA = no opportunity), per-visit
## detection probabilities p and occupancy probability psi:
##
##   L = psi * prod_{opp} p^y (1-p)^(1-y)  +  (1 - psi) * 1{all y == 0}
##
## A site with no detection opportunity contributes L = 1 (dropped upstream).
## ---------------------------------------------------------------------------

single_site_loglik <- function(y, p, psi) {
  opp <- !is.na(y)
  yo  <- y[opp]
  po  <- p[opp]
  if (length(yo) == 0) return(0)             # no information
  ll_cond <- sum(yo * log(po) + (1 - yo) * log1p(-po))
  l <- psi * exp(ll_cond)
  if (all(yo == 0)) l <- l + (1 - psi)
  log(l)
}

## Rebuild psi_i and p_ij from the fitted coefficients and the same design
## matrices unmarked uses, then sum the per-site log-likelihoods by hand.
## Note: umf@obsCovs is a long data.frame (M*J rows, site-major order).
hand_nll <- function(fit, umf, state_rhs, det_rhs) {
  y  <- unmarked::getY(umf)
  sc <- as.data.frame(unmarked::siteCovs(umf))
  oc <- as.data.frame(umf@obsCovs)
  cf_s <- coef(fit, type = "state")   # unmarked S4 generic
  cf_d <- coef(fit, type = "det")

  Xs  <- stats::model.matrix(stats::as.formula(paste("~", state_rhs)), sc)
  stopifnot(ncol(Xs) == length(cf_s))
  psi <- as.numeric(plogis(Xs %*% cf_s))

  M <- nrow(y); J <- ncol(y)
  stopifnot(nrow(oc) == M * J,
            identical(is.na(oc[[1]]), is.na(as.vector(t(y)))))
  ## NA rows (no detection opportunity) are never used in the likelihood;
  ## zero them so model.matrix keeps every row regardless of na.action.
  oc0 <- oc
  oc0[is.na(oc0)] <- 0
  Xd <- stats::model.matrix(stats::as.formula(paste("~", det_rhs)), oc0)
  stopifnot(nrow(Xd) == M * J, ncol(Xd) == length(cf_d))
  p <- matrix(plogis(as.numeric(Xd %*% cf_d)), nrow = M, byrow = TRUE)

  ll <- vapply(seq_len(M),
               function(i) single_site_loglik(y[i, ], p[i, ], psi[i]),
               numeric(1))
  -sum(ll)
}

## Compare the hand-computed negative log-likelihood at the MLE with the
## value unmarked reports from its own optimiser.
likelihood_crosscheck <- function(fit, umf, state_rhs, det_rhs, diag,
                                  tol = 1e-6) {
  if (!diag$ok) return(NULL)
  hand <- hand_nll(fit, umf, state_rhs, det_rhs)
  pkg  <- fit@opt$value
  list(hand_nll = hand, pkg_nll = pkg,
       abs_diff = abs(hand - pkg),
       passed = is.finite(hand) && abs(hand - pkg) < tol)
}
