"""Home Assistant app directory.

This package marker exists so the service code under ``app/`` can be
imported as ``android_timeline.app`` from the repository root during
testing. Inside the container the app directory *is* the working
directory, so the same modules import as ``app.*`` -- which is why every
import within ``app/`` is relative.
"""
