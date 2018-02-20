@echo off
set ARDUINO_NAME=BaseNodeRpc
set PLATFORMIO_ENVS=uno pro8MHzatmega328 teensy31 micro megaADK megaatmega2560

setlocal enableextensions
md "%PREFIX%"\Library\include\Arduino
FOR %%i IN (%PLATFORMIO_ENVS%) DO (
  md "%PREFIX%"\Library\bin\platformio\%PKG_NAME%\%%i
)
endlocal

REM Generate Arduino `library.properties` file
python -m paver generate_arduino_library_properties

REM Generate Arduino code
python -m paver generate_all_code
if errorlevel 1 exit 1

REM Copy Arduino library to Conda include directory
xcopy /S /Y /I /Q "%SRC_DIR%"\lib\%ARDUINO_NAME% "%PREFIX%"\Library\include\Arduino\%ARDUINO_NAME%
if errorlevel 1 exit 1

REM Build firmware
python -m paver build_firmware
if errorlevel 1 exit 1

REM Copy compiled firmware to Conda bin directory
copy "%SRC_DIR%"\platformio.ini "%PREFIX%"\Library\bin\platformio\%PKG_NAME%
FOR %%i IN (%PLATFORMIO_ENVS%) DO (
  copy "%SRC_DIR%"\.pioenvs\%%i\firmware.hex "%PREFIX%"\Library\bin\platformio\%PKG_NAME%\%%i\firmware.hex
)
if errorlevel 1 exit 1

REM Generate `setup.py` from `pavement.py` definition.
python -m paver generate_setup

REM Install source directory as Python package.
python setup.py install --single-version-externally-managed --record record.txt
if errorlevel 1 exit 1
