#!/usr/bin/env Rscript
## Run the occupancy pipeline, then the validation suite.
## Usage:  Rscript run_pipeline.R
args <- commandArgs(trailingOnly = FALSE)
this <- sub("--file=", "", grep("--file=", args, value = TRUE))
if (length(this)) setwd(dirname(normalizePath(this)))

targets::tar_make()
source(file.path("tests", "validate.R"))
