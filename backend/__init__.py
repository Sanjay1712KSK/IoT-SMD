"""Smart Door Monitor backend."""
import os

# Hide OpenCV's harmless "[ WARN:0 ] ... Targets are not supported" DNN messages.
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
