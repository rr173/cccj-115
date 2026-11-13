## ---------------------------------------------------------------------------
## Fully synthetic survey data for a protected-area occupancy study.
## Three linked tables:
##   * site attributes  (site_id, zone, season, versioned covariates)
##   * visit records    (site_id x visit_id, completion / device / detection)
##   * observer & device configuration
## Nothing here is read from disk; every value is simulated.
## ---------------------------------------------------------------------------

sim_observers <- function() {
  data.frame(
    observer_id = c("OBS_A", "OBS_B", "OBS_C"),
    experience  = c("senior", "mid", "novice"),
    ## true detection skill on the logit scale (simulation truth only)
    skill_logit = c(0.9, 0.0, -0.9),
    stringsAsFactors = FALSE
  )
}

sim_devices <- function() {
  data.frame(
    device_id    = c("CAM", "ARU"),
    device_type  = c("camera", "acoustic"),
    effect_logit = c(0.0, 0.4),          # acoustic array slightly more sensitive
    stringsAsFactors = FALSE
  )
}

## Site attributes with *versioned* covariates.
##   *_v1 : original covariate release
##   *_v2 : revised release (updated habitat map for a subset of sites)
## pin_covariates() freezes one release per analysis version.
sim_sites <- function(n_sites = 120, seed = 1) {
  set.seed(seed)
  zones   <- paste0("Z", 1:4)
  seasons <- c("2024-dry", "2024-wet")
  sites <- data.frame(
    site_id   = sprintf("S%03d", seq_len(n_sites)),
    zone      = rep(zones,   length.out = n_sites),
    season    = rep(seasons, length.out = n_sites),
    elev_v1   = round(rnorm(n_sites, 800, 150), 1),
    forest_v1 = round(pmin(pmax(rbeta(n_sites, 2, 1.5), 0), 1), 3),
    stringsAsFactors = FALSE
  )
  set.seed(seed + 100)
  revised <- sample(seq_len(n_sites), ceiling(0.2 * n_sites))
  sites$elev_v2   <- sites$elev_v1
  sites$forest_v2 <- sites$forest_v1
  sites$elev_v2[revised]   <- round(sites$elev_v1[revised] + rnorm(length(revised), 0, 60), 1)
  sites$forest_v2[revised] <- round(pmin(pmax(sites$forest_v1[revised] +
                                                rnorm(length(revised), 0, 0.1), 0), 1), 3)
  sites
}

## Visit records.  A visit is a *detection opportunity* only when it was
## completed AND the device worked.  Three distinct states are simulated:
##   completed & device_ok & detection = 0   -> valid non-detection (0)
##   completed & device_ok & detection = 1   -> detection (1)
##   !completed (cancelled) or !device_ok    -> no opportunity (NA, never 0)
## Some sites simply have fewer planned visits (missing visits, not zeros).
sim_visits <- function(sites, observers, devices, J = 4, seed = 2,
                       p_cancel = 0.07, p_fail = 0.08, p_fewer = 0.15) {
  set.seed(seed)
  zelev     <- as.numeric(scale(sites$elev_v1))
  logit_psi <- -0.3 + 0.8 * zelev + 1.2 * sites$forest_v1
  psi       <- plogis(logit_psi)
  z         <- rbinom(nrow(sites), 1, psi)          # true occupancy state

  rows <- vector("list", nrow(sites) * J)
  k <- 0L
  for (i in seq_len(nrow(sites))) {
    sid <- sites$site_id[i]
    n_visits <- J
    ## S010 / S020 / S030 are reserved for injected data problems: keep J visits
    if (runif(1) < p_fewer && !sid %in% c("S010", "S020", "S030"))
      n_visits <- sample(2:(J - 1), 1)
    base_date <- as.Date("2024-03-01") + sample(0:20, 1)
    for (j in seq_len(n_visits)) {
      completed <- runif(1) > p_cancel
      device_ok <- completed && runif(1) > p_fail
      ob <- sample(observers$observer_id, 1)
      dv <- sample(devices$device_id, 1)
      p_det <- plogis(-0.2 +
                        observers$skill_logit[match(ob, observers$observer_id)] +
                        devices$effect_logit[match(dv, devices$device_id)])
      detection <- NA_integer_
      if (completed && device_ok)
        detection <- rbinom(1, 1, z[i] * p_det)
      k <- k + 1L
      rows[[k]] <- data.frame(
        site_id    = sid,
        visit_id   = j,
        visit_date = base_date + (j - 1) * 7L + sample(0:2, 1),
        completed  = completed,
        device_ok  = device_ok,
        observer_id = ob,
        device_id   = dv,
        detection   = detection,
        stringsAsFactors = FALSE
      )
    }
  }
  visits <- do.call(rbind, rows[lengths(rows) > 0])
  rownames(visits) <- NULL
  list(visits = visits,
       truth  = data.frame(site_id = sites$site_id, psi_true = psi, z = z))
}

## Inject the three required data problems (deterministic targets):
##   S005 : duplicate site key with conflicting attributes
##   S010 : visit dates inverted (visit 3 dated before visit 2)
##   S020 : two contradictory detection records for the same visit
##   S030 : detection recorded on a cancelled visit (no opportunity)
inject_problems <- function(sites, visits) {
  dup <- sites[sites$site_id == "S005", , drop = FALSE]
  dup$forest_v1 <- dup$forest_v1 + 0.2
  sites_bad <- rbind(sites, dup)

  idx <- which(visits$site_id == "S010" & visits$visit_id %in% c(2L, 3L))
  visits$visit_date[idx] <- rev(visits$visit_date[idx])   # now visit2 > visit3

  v1 <- which(visits$site_id == "S020" & visits$visit_id == 1L)
  visits$completed[v1] <- TRUE
  visits$device_ok[v1] <- TRUE
  visits$detection[v1] <- 0L
  conflict <- visits[v1, ]
  conflict$detection <- 1L                                # contradictory record
  visits_bad <- rbind(visits, conflict)

  v2 <- which(visits_bad$site_id == "S030" & visits_bad$visit_id == 2L)
  visits_bad$completed[v2]  <- FALSE                      # cancelled ...
  visits_bad$device_ok[v2]  <- FALSE
  visits_bad$detection[v2]  <- 1L                         # ... yet "detected"

  list(sites = sites_bad, visits = visits_bad)
}

## One-call synthetic survey.
sim_survey <- function(n_sites = 120, J = 4, seed = 20241113) {
  observers <- sim_observers()
  devices   <- sim_devices()
  sites     <- sim_sites(n_sites = n_sites, seed = seed)
  sv        <- sim_visits(sites, observers, devices, J = J, seed = seed + 1)
  bad       <- inject_problems(sites, sv$visits)
  list(sites = bad$sites, visits = bad$visits,
       observers = observers, devices = devices, truth = sv$truth)
}
