# A stand-in for the phone, for local development only.
#
# CI does NOT use this: the cross-repository workflow installs the real
# collector from android-timeline-termux and runs its actual uploader.
# This exists so the pipeline can be exercised on a laptop.
FROM python:3.13-alpine

WORKDIR /work
COPY upload.py ./

ENTRYPOINT ["python", "/work/upload.py"]
