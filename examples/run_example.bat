@echo off
REM Worked example for AM_ImageAndMask_to_cubemap_v4.py
REM Run from the examples/ directory.

echo Processing back lens...
python ..\AM_ImageAndMask_to_cubemap_v4.py ^
  --amlenscal Osmo360_back_adjusted.xml ^
  --lenslabel "Lens-back" ^
  --directoryfisheyeimages back\images ^
  --directoryfisheyemasks back\masks ^
  --facewidth 2100 ^
  --outputdir output

if errorlevel 1 (
  echo.
  echo Back lens processing failed. Stopping.
  exit /b 1
)

echo.
echo Processing front lens...
python ..\AM_ImageAndMask_to_cubemap_v4.py ^
  --amlenscal Osmo360_front_adjusted.xml ^
  --lenslabel "Lens-front" ^
  --directoryfisheyeimages front\images ^
  --directoryfisheyemasks front\masks ^
  --facewidth 2100 ^
  --outputdir output

if errorlevel 1 (
  echo.
  echo Front lens processing failed.
  exit /b 1
)

echo.
echo Done. Compare output\Lens-back\images\^<frame^>\^<frame^>_dir_plusZ.png and
echo output\Lens-front\images\^<frame^>\^<frame^>_dir_plusZ.png against expected_output\.
