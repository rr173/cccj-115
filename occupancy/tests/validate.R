## ---------------------------------------------------------------------------
## Validation suite.  Run after targets::tar_make() from the project root:
##   Rscript tests/validate.R
## Exits non-zero if any check fails.
## ---------------------------------------------------------------------------

library(targets)
suppressPackageStartupMessages(library(unmarked))   # S4 generics: coef/vcov
for (f in list.files("R", pattern = "\\.R$", full.names = TRUE)) source(f)

failures <- character(0)
check <- function(name, expr) {
  ok <- isTRUE(tryCatch(force(expr), error = function(e) {
    message(sprintf("  [error in %s] %s", name, conditionMessage(e)))
    FALSE
  }))
  cat(sprintf("[%s] %s\n", if (ok) "PASS" else "FAIL", name))
  if (!ok) failures <<- c(failures, name)
  invisible(ok)
}

suppressMessages(tar_make(callr_function = NULL))   # ensure pipeline is current

hist  <- tar_read(hist)
diag  <- tar_read(diag)
preds <- tar_read(preds)
fit   <- tar_read(fit)
umf   <- tar_read(umf)
sim   <- tar_read(sim)
sites_q  <- tar_read(sites_q)
visits_q <- tar_read(visits_q)
sites_pinned <- tar_read(sites_pinned)
crosscheck <- tar_read(crosscheck)
result <- tar_read(result)

## 1. All-non-detection sites still get a non-zero occupancy estimate --------
all_zero_sites <- rownames(hist$y)[apply(hist$y, 1, function(r)
  all(r[!is.na(r)] == 0) && any(!is.na(r)))]
check("all-nondetection sites exist in synthetic data", length(all_zero_sites) > 0)
p0 <- preds$psi[preds$site_id %in% all_zero_sites]
check("all-nondetection site has 0 < psi < 1", all(p0 > 0 & p0 < 1))
ll <- single_site_loglik(y = c(0, 0, 0), p = c(0.5, 0.5, 0.5), psi = 0.5)
check("hand likelihood of all-zero history includes (1-psi) term",
      isTRUE(all.equal(exp(ll), 0.5 * 0.5^3 + 0.5)))

## 2. Valid non-detection vs device failure vs cancellation ------------------
mini <- data.frame(
  site_id = "SX", visit_id = 1:3,
  visit_date = as.Date("2024-03-01") + 0:2,
  completed  = c(TRUE, TRUE, FALSE),
  device_ok  = c(TRUE, FALSE, FALSE),
  observer_id = "OBS_A", device_id = "CAM",
  detection  = c(0L, NA, NA), stringsAsFactors = FALSE)
h2 <- build_history(mini, data.frame(site_id = "SX"), J = 3)
check("valid non-detection = 0, device failure / cancel = NA",
      identical(as.vector(h2$y), c(0L, NA, NA)))

## 3. Observer detection-rate differences recovered ---------------------------
cf_d <- coef(fit, type = "det")
getc <- function(pat) unname(cf_d[grepl(pat, names(cf_d))])
check("observer skill ordering recovered (novice < mid < senior)",
      getc("obs_low") < getc("obs_mid") && getc("obs_mid") < 0)

## 4. Missing visits are NA, not zero -----------------------------------------
n_visits <- table(factor(sim$visits$site_id, levels = unique(sim$visits$site_id)))
fewer <- names(n_visits)[n_visits < 4]
fewer <- setdiff(fewer, c("S005", "S010", "S020", "S030"))
fewer <- intersect(fewer, rownames(hist$y))   # still analysed (not all visits lost)
check("sites with fewer planned visits exist", length(fewer) > 0)
if (length(fewer) > 0) {
  r <- hist$y[fewer[1], ]
  check("missing visit coded NA (not 0)",
        any(is.na(r)) && all(r[!is.na(r)] %in% c(0L, 1L)))
}

## 5. Defective records land in the quarantine tables -------------------------
check("duplicate site key quarantined (S005)",
      any(sites_q$quarantine$site_id == "S005") &&
        !("S005" %in% sites_q$clean$site_id))
q <- visits_q$quarantine
check("date inversion quarantined (S010)",
      any(q$site_id == "S010" & grepl("date_inversion", q$reason)))
check("conflicting detection quarantined (S020 visit 1)",
      any(q$site_id == "S020" & q$visit_id == 1 &
            grepl("conflicting_detection", q$reason)))
check("detection without opportunity quarantined (S030)",
      any(q$site_id == "S030" & grepl("detection_without_opportunity", q$reason)))
check("conflicting visit becomes NA, not 0",
      is.na(hist$y["S020", "v1"]))

## 6. A single unique detection history is not identifiable -------------------
## All 40 sites share the history (1,1,1,1): psi -> 1 and p -> 1 hit the
## boundary, the Hessian is (near-)singular -> estimates must be withheld.
set.seed(7)
y_same <- matrix(1L, nrow = 40, ncol = 4)
rownames(y_same) <- sprintf("U%02d", 1:40)
umf_same <- unmarked::unmarkedFrameOccu(y = y_same)
fit_same  <- fit_occupancy(umf_same, "1", "1", seed = 7)
diag_same <- diagnose_fit(fit_same)
check("single detection history -> estimates withheld",
      !diag_same$ok)
check("withheld result carries localising diagnostics",
      !diag_same$ok && (length(diag_same$problems) > 0 || !is.null(diag_same$message)))

## 7. Covariate version change -> new analysis identity -----------------------
p1 <- pin_covariates(sites_q$clean, "v1")
p2 <- pin_covariates(sites_q$clean, "v2")
check("v1 vs v2 covariates differ for revised sites",
      !identical(p1$forest, p2$forest))
ssid1 <- site_set_id(rownames(hist$y))
fid1  <- formula_id("elev + forest", "obs_mid + obs_low + dev_aru")
aid1  <- analysis_id(ssid1, fid1, "v1")
aid2  <- analysis_id(ssid1, fid1, "v2")
aid3  <- analysis_id(ssid1, formula_id("elev", "obs_mid + obs_low + dev_aru"), "v1")
check("covariate version change -> new analysis_id", aid1 != aid2)
check("formula change -> new analysis_id", aid1 != aid3)
check("same inputs -> identical analysis_id (reuse)",
      aid1 == analysis_id(ssid1, fid1, "v1"))
check("pipeline analysis_id matches v1 identity", tar_read(aid) == aid1)

## 8. Fixed-seed fits are reproducible -----------------------------------------
fit_a <- fit_occupancy(umf, "elev + forest", "obs_mid + obs_low + dev_aru", seed = 99)
fit_b <- fit_occupancy(umf, "elev + forest", "obs_mid + obs_low + dev_aru", seed = 99)
check("same seed -> identical coefficients",
      isTRUE(all.equal(coef(fit_a), coef(fit_b), tolerance = 1e-12)))

## 9. Hand-written likelihood matches unmarked ----------------------------------
check("hand nll matches unmarked nll", isTRUE(crosscheck$passed))

## 10. Probability ranges and prediction intervals -------------------------------
check("psi predictions within [0,1]", all(preds$psi >= 0 & preds$psi <= 1))
check("Wald interval ordered and within [0,1]",
      all(preds$wald_lwr >= 0 & preds$wald_upr <= 1 &
            preds$wald_lwr <= preds$psi & preds$psi <= preds$wald_upr))
check("bootstrap interval ordered and within [0,1]",
      all(preds$boot_lwr >= 0 & preds$boot_upr <= 1 &
            preds$boot_lwr <= preds$boot_upr))
check("published batch carries vcov with finite entries",
      result$status == "published" && all(is.finite(result$vcov)))

cat("\n")
if (length(failures)) {
  cat("FAILED:", paste(failures, collapse = ", "), "\n")
  quit(status = 1)
}
cat("All validation checks passed.\n")
