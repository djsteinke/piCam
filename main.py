import io
import logging
import time
from threading import Condition

from flask import Flask, Response, render_template_string
from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder

# We'll use a custom output class similar to what's used in picamera2 examples
# for efficient streaming.

# --- Configuration ---
CAMERA_RESOLUTION = (640, 480)
FRAME_RATE = 25  # You can try adjusting this
JPEG_QUALITY = 70  # 1-100
SERVER_PORT = 5000

# --- Global Camera and Streaming Output ---
picam2 = None


# This class will hold the latest frame and notify waiting threads
class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()  # Notify all waiting threads


# --- Flask Application ---
app = Flask(__name__)


def initialize_camera_and_start_streaming():
    global picam2, output_stream
    try:
        picam2 = Picamera2()

        # Optional: Print camera info for debugging
        # logging.info(Picamera2.global_camera_info())
        # if not picam2.cameras:
        #     logging.error("No cameras found!")
        #     return False

        video_config = picam2.create_video_configuration(
            main={"size": CAMERA_RESOLUTION},
            controls={"FrameRate": FRAME_RATE}
        )
        picam2.configure(video_config)

        # Initialize our custom streaming output
        output_stream = StreamingOutput()

        # Create a JpegEncoder
        encoder = JpegEncoder(q=JPEG_QUALITY)

        # Start recording to our custom output
        # The encoder will write JPEG frames to output_stream.write()
        picam2.start_recording(encoder, output_stream)

        logging.info(f"Camera initialized. Streaming at {CAMERA_RESOLUTION} resolution, {FRAME_RATE} FPS.")
        logging.info(f"Camera controls: {picam2.camera_controls}")

        # Allow some time for the camera to warm up and produce the first frame
        time.sleep(1.5)  # Increased slightly for stability
        return True
    except Exception as e:
        logging.error(f"Failed to initialize camera or start recording: {e}", exc_info=True)
        if picam2:
            try:
                picam2.close()  # Ensure camera is closed on error
            except Exception as close_e:
                logging.error(f"Error closing camera during error handling: {close_e}")
        picam2 = None  # Reset global
        return False


def generate_frames():
    """Generator function to yield frames for the MJPEG stream."""
    global output_stream
    if not picam2 or not output_stream:
        logging.error("Camera or output stream not initialized for generating frames.")
        # Yield a placeholder or error image if you want
        # For now, just stop if not initialized
        return

    while True:
        try:
            with output_stream.condition:
                output_stream.condition.wait()  # Wait until a new frame is available
                frame = output_stream.frame
            if frame:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            else:
                # This might happen if the stream stops or an error occurs
                logging.warning("Frame was None, skipping.")
                time.sleep(0.1)  # Avoid busy-looping if frames stop
        except Exception as e:
            logging.error(f"Error in generate_frames: {e}", exc_info=True)
            break  # Exit the loop on error to prevent continuous logging


@app.route('/')
def index():
    """Serves the main HTML page with the video feed."""
    # Using render_template_string to keep HTML within the Python file for simplicity
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
            // Optional: Add a simple error handler for the image
            const img = document.getElementById('video_stream');
            img.onerror = function() {
                this.alt = 'Video stream failed to load. Check Pi console for errors.';
                // You could also try to reload the image or display a message
                // For example: document.body.innerHTML += "<p style='color:red;'>Stream error. Please check server.</p>";
            };
        </script>
    </body>
    </html>
    """
    return render_template_string(html_content, width=CAMERA_RESOLUTION[0], height=CAMERA_RESOLUTION[1])


@app.route('/video_feed')
def video_feed():
    """Route that serves the MJPEG video stream."""
    if not picam2 or not output_stream:
        logging.error("Video feed requested, but camera is not ready.")
        return "Camera not ready", 503  # Service Unavailable

    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    if not initialize_camera_and_start_streaming():
        logging.error("Application cannot start due to camera initialization failure.")
        # Optionally, you could exit here or try to run a limited version of the app
        # For this example, we'll prevent Flask from starting if the camera fails.
    else:
        try:
            logging.info(f"Starting Flask server on http://0.0.0.0:{SERVER_PORT}")
            # threaded=True allows Flask to handle multiple requests concurrently (e.g., serving the stream and other pages)
            # and ensures the camera's background recording thread doesn't block Flask.
            app.run(host='0.0.0.0', port=SERVER_PORT, threaded=True, debug=False)
        except KeyboardInterrupt:
            logging.info("Keyboard interrupt received. Shutting down...")
        except Exception as e:
            logging.error(f"An error occurred while running the Flask app: {e}", exc_info=True)
        finally:
            if picam2:
                logging.info("Stopping camera recording...")
                try:
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
