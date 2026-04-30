#!/usr/bin/env bash
# Worked example for AM_ImageAndMask_to_cubemap_v4.py
# Run from the examples/ directory.

set -e  # exit on any error

echo "Processing back lens..."
python ../AM_ImageAndMask_to_cubemap_v4.py \
  --amlenscal Osmo360_back_adjusted.xml \
  --lenslabel "Lens-back" \
  --directoryfisheyeimages back/images \
  --directoryfisheyemasks back/masks \
  --facewidth 2100 \
  --outputdir output

echo
echo "Processing front lens..."
python ../AM_ImageAndMask_to_cubemap_v4.py \
  --amlenscal Osmo360_front_adjusted.xml \
  --lenslabel "Lens-front" \
  --directoryfisheyeimages front/images \
  --directoryfisheyemasks front/masks \
  --facewidth 2100 \
  --outputdir output

echo
echo "Done. Compare output/Lens-back/images/<frame>/<frame>_dir_plusZ.png and"
echo "output/Lens-front/images/<frame>/<frame>_dir_plusZ.png against expected_output/."
