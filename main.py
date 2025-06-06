import io
import logging
import time
from threading import Condition

from flask import Flask, Response, render_template_string
from picamera2 import Picamera2, Preview
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput

# --- Configuration ---
CAMERA_RESOLUTION = (640, 480)
FRAME_RATE = 10
JPEG_QUALITY = 50
SERVER_PORT = 31001

# --- Global Camera and Streaming Output ---
picam2 = None
output_stream = None


# This class will hold the latest frame and notify waiting threads
class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        super().__init__() # It's good practice to call super().__init__
        self.frame = None
        self.condition = Condition()

    def write(self, buf):
        # The write method should return the number of bytes written.
        bytes_written = len(buf)
        with self.condition:
            self.frame = buf
            self.condition.notify_all()  # Notify all waiting threads
        return bytes_written # Return the number of bytes written


# --- Flask Application ---
app = Flask(__name__)


def initialize_camera_and_start_streaming():
    global picam2, output_stream
    try:
        picam2 = Picamera2()

        config = picam2.create_still_configuration(
            main={"size": CAMERA_RESOLUTION},
            controls={"FrameRate": FRAME_RATE}
        )
        picam2.configure(config)
        picam2.start_preview(Preview.NULL)
        encoder = H264Encoder(10000000)
        picam2.start()

        video_output = FfmpegOutput("-f mpegts udp://192.168.0.154:31001/video")
        picam2.start_recording(encoder, output=video_output, name="video.ts")

        output_stream = StreamingOutput()

        logging.info(f"Camera initialized. Streaming at {CAMERA_RESOLUTION} resolution, {FRAME_RATE} FPS.")
        logging.info(f"Camera controls: {picam2.camera_controls}")

        time.sleep(1.5)
        return True
    except Exception as e:
        # Ensure the full exception (including "must pass output") is logged
        logging.error(f"Failed to initialize camera or start recording: {e}", exc_info=True)
        if picam2:
            try:
                # Attempt to stop recording if it somehow started before error
                picam2.close()
            except Exception as close_e:
                logging.error(f"Error during camera cleanup after initialization failure: {close_e}")
        picam2 = None
        output_stream = None # Also reset output_stream if initialization fails
        return False


def generate_frames():
    """Generator function to yield frames for the MJPEG stream."""
    global output_stream # Ensure we're using the global output_stream
    if not picam2: # or not output_stream or not picam2.started: # Added check for picam2.started
        logging.error("Camera or output stream not initialized/started for generating frames.")
        return

    while True:
        try:
            frame = picam2.capture_array("main")
            if frame:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame.tobytes() + b'\r\n')
            else:
                # This might happen if the stream stops or an error occurs
                logging.warning("Frame was None after condition signaled, skipping.")
                time.sleep(0.01) # Brief pause
        except Exception as e:
            logging.error(f"Error in generate_frames: {e}", exc_info=True)
            break


@app.route('/')
def index():
    """Serves the main HTML page with the video feed."""
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Raspberry Pi Camera Stream (Picamera2)</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background-color: #f4f4f4; text-align: center; }
            h1 { color: #333; }
            img { border: 2px solid #333; margin-top: 20px; }
            p { color: #555; }
        </style>
    </head>
    <body>
        <h1>Raspberry Pi Camera Live Stream</h1>
        <img id="video_stream" src="{{ url_for('video_feed') }}" width="{{width}}" height="{{height}}" alt="Loading video stream...">
        <p>Powered by Picamera2 and Flask.</p>
        <script>
            const img = document.getElementById('video_stream');
            img.onerror = function() {
                this.alt = 'Video stream failed to load. Check Pi console for errors.';
            };
        </script>
    </body>
    </html>
    """
    return render_template_string(html_content, width=CAMERA_RESOLUTION[0], height=CAMERA_RESOLUTION[1])


@app.route('/video_feed')
def video_feed():
    """Route that serves the MJPEG video stream."""
    if not picam2:# or not output_stream or not picam2.started: # Added check for picam2.started
        logging.error("Video feed requested, but camera is not ready or not started.")
        return "Camera not ready", 503

    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    if not initialize_camera_and_start_streaming():
        logging.error("Application cannot start due to camera initialization failure.")
    else:
        try:
            logging.info(f"Starting Flask server on http://0.0.0.0:{SERVER_PORT}")
            app.run(host='0.0.0.0', port=SERVER_PORT, threaded=True, debug=False)
        except KeyboardInterrupt:
            logging.info("Keyboard interrupt received. Shutting down...")
        except Exception as e:
            logging.error(f"An error occurred while running the Flask app: {e}", exc_info=True)
        finally:
            if picam2:
                logging.info("Shutting down camera...")
                try:
                    if picam2.started: # Check if recording was started before trying to stop
                        picam2.stop_recording()
                        logging.info("Camera recording stopped.")
                except Exception as e:
                    logging.error(f"Error stopping recording: {e}", exc_info=True)
                try:
                    picam2.close()
                    logging.info("Camera closed.")
                except Exception as e:
                    logging.error(f"Error closing camera: {e}", exc_info=True)
            logging.info("Application terminated.")