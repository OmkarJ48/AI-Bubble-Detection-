#!/usr/bin/env python3
"""
Raspberry Pi 5 Camera Livestream Server

"""
bubble_id_counter = 0
tracked_bubbles = {}
last_record_time = 0
COOLDOWN = 2
output = None
frame_count = 0
motion_timer = 0
motion_recording = False
MOTION_RECORD_SECONDS = 3
prev_frame = None
motion_detected = False
from ultralytics import YOLO
import io
import threading
import logging
import time
import numpy as np
import cv2
from picamera2.outputs import FileOutput
from picamera2 import Picamera2
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template_string, Response, jsonify
from PIL import Image

model = YOLO("yolov8n.pt")  # Load the YOLOv8n model



objects = {}

def update_objects(ids):
    now = time.time()

    for obj_id in objects:
        
        objects[obj_id]["seen"] = False
    
    for obj_id in ids:
        if obj_id not in objects:
            objects[obj_id] = {"first_seen": now, "last_seen": now, "seen": True}
        else:
            objects[obj_id]["last_seen"] = now
            objects[obj_id]["seen"] = True
    
    for obj_id in list(objects.keys()):
        if not objects[obj_id]["seen"]: 
           if now - objects[obj_id]["last_seen"] > 2:
            del objects[obj_id]
    
   
    

class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
              
            self.frame = buf
            self.condition.notify_all()
   


try:
    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder, JpegEncoder, MJPEGEncoder
    from picamera2.outputs import FfmpegOutput, FileOutput
except ImportError:
    print("Error: picamera2 is not installed")
    exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Flask app initialization
app = Flask(__name__)

# Global variables
camera = None

frame_lock = threading.Lock()
streaming = False
recording = False
recording_output = None


# HTML template for the web interface
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>Raspberry Pi 5 Camera Livestream</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        
        .container {
            background: white;
            border-radius: 12px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            max-width: 1200px;
            width: 100%;
            padding: 30px;
        }
        
        h1 {
            text-align: center;
            color: #333;
            margin-bottom: 10px;
            font-size: 2em;
        }
        
        .status {
            text-align: center;
            color: #666;
            margin-bottom: 20px;
            font-size: 0.9em;
        }
        
        .status.live {
            color: #27ae60;
            font-weight: bold;
        }
        
        .status.live::before {
            content: "● ";
            color: #27ae60;
            font-size: 1.2em;
            margin-right: 5px;
        }
        
        .video-container {
            position: relative;
            width: 100%;
            background: #000;
            border-radius: 8px;
            overflow: hidden;
            margin-bottom: 20px;
            aspect-ratio: 16 / 9;
        }
        
        #stream {
            width: 100%;
            height: 100%;
            display: block;
            background: #000;
        }
        
        .controls {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 10px;
            margin-bottom: 20px;
        }
        
        button {
            padding: 12px 20px;
            font-size: 1em;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 600;
            transition: all 0.3s ease;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        button:hover:not(:disabled) {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0, 0, 0, 0.2);
        }
        
        button:active:not(:disabled) {
            transform: translateY(0);
        }
        
        button:disabled {
            opacity: 0.6;
            cursor: not-allowed;
        }
        
        .btn-capture {
            background: #3498db;
            color: white;
        }
        
        .btn-capture:hover:not(:disabled) {
            background: #2980b9;
        }
        
        .btn-record {
            background: #e74c3c;
            color: white;
        }
        
        .btn-record:hover:not(:disabled) {
            background: #c0392b;
        }
        
        .btn-record.recording {
            background: #27ae60;
            animation: pulse 1s infinite;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
        }
        
        .info {
            background: #e8f4f8;
            border-left: 4px solid #3498db;
            padding: 12px;
            border-radius: 4px;
            margin-top: 10px;
            font-size: 0.9em;
            color: #555;
            display: none;
        }
        
        .error {
            background: #fadbd8;
            border-left: 4px solid #e74c3c;
            padding: 12px;
            border-radius: 4px;
            margin-top: 10px;
            font-size: 0.9em;
            color: #c0392b;
            display: none;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎥 Raspberry Pi 5 Livestream</h1>
        <div class="status live" id="status">LIVE</div>
        
        <div class="video-container">
            <img id="stream" src="/video_feed" alt="Camera Stream">
        </div>
        
        <div class="controls">
            <button class="btn-capture" onclick="captureImage()">📸 Capture</button>
            <button class="btn-record" id="recordBtn" onclick="toggleRecording()">🔴 Record</button>
        </div>
        
        <div id="message" class="info"></div>
        <div id="error" class="error"></div>
    </div>
    
    <script>
        let isRecording = false;
        let streamTimeout;
        
        // Keep refreshing the stream
        function refreshStream() {
            const img = document.getElementById('stream');
            const now = new Date().getTime();
            img.src = '/video_feed?t=' + now;
            streamTimeout = setTimeout(refreshStream, 200);
        }
        
        function captureImage() {
            const button = event.target;
            button.disabled = true;
            
            fetch('/capture')
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        showMessage('📸 Image captured: ' + data.filename);
                    } else {
                        showError('Failed to capture image: ' + data.error);
                    }
                })
                .catch(error => {
                    showError('Failed to capture image: ' + error);
                })
                .finally(() => {
                    button.disabled = false;
                });
        }
        
        function toggleRecording() {
            const button = event.target;
            button.disabled = true;
            
            const action = isRecording ? 'stop_recording' : 'start_recording';
            
            fetch('/' + action, { method: 'POST' })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        isRecording = !isRecording;
                        if (isRecording) {
                            button.classList.add('recording');
                            button.textContent = '⏹️ Stop Recording';
                            showMessage('🔴 Recording started: ' + data.filename);
                        } else {
                            button.classList.remove('recording');
                            button.textContent = '🔴 Record';
                            showMessage('Recording stopped');
                        }
                    } else {
                        showError('Failed: ' + data.error);
                    }
                })
                .catch(error => {
                    showError('Failed to toggle recording: ' + error);
                })
                .finally(() => {
                    button.disabled = false;
                });
        }
        
        function showMessage(msg) {
            const msgDiv = document.getElementById('message');
            msgDiv.textContent = msg;
            msgDiv.style.display = 'block';
            setTimeout(() => {
                msgDiv.style.display = 'none';
            }, 3000);
        }
        
        function showError(msg) {
            const errDiv = document.getElementById('error');
            errDiv.textContent = msg;
            errDiv.style.display = 'block';
        }
        
        // Start streaming when page loads
        window.addEventListener('load', refreshStream);
        window.addEventListener('beforeunload', () => clearTimeout(streamTimeout));
    </script>
</body>
</html>
'''


@app.route('/')
def index():
    """Serve the main HTML page."""
    return render_template_string(HTML_TEMPLATE)


@app.route('/video_feed')
def video_feed():
    """Stream individual JPEG frames."""
    def generate():
        """Generate frames continuously."""
        global output
        while True:
            with output.condition:
                output.condition.wait()
                frame = output.frame     
           
            yield (
                b'--frame\r\n'
                b'Content-Type: image/jpeg\r\n\r\n' +
                frame + b'\r\n'           
                
            )
                    
    return Response(generate(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/capture')
def capture():
    """Capture a still image."""
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"image_{timestamp}.jpg"
        
        request_obj = camera.capture_request()
        request_obj.save("main", filename)
        request_obj.release()
        
        logger.info(f"Image captured: {filename}")
        return jsonify({'success': True, 'filename': filename})
    except Exception as e:
        logger.error(f"Error capturing image: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/start_recording', methods=['POST'])
def start_recording():
    global recording, recording_output

    if recording:
        return jsonify({'success': False, 'error': 'Already recording'})

    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"recording_{timestamp}.mp4"


        return jsonify({'success': True, 'filename': filename})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/stop_recording', methods=['POST'])
def stop_recording():
    """Stop recording video."""
    global recording, recording_output
    
    if not recording:
        return jsonify({'success': False, 'error': 'Not recording'})
    
    try:
        camera.stop_recording()
        recording = False
        logger.info("Recording stopped")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error stopping recording: {e}")
        return jsonify({'success': False, 'error': str(e)})


def frame_capture_thread():
    """Continuously capture frames from camera."""
    global prev_frame, motion_recording, motion_timer, motion_detected, last_record_time, tracked_bubbles, bubble_id_counter
    
    print("THREAD STARTED")
    logger.info("Frame capture thread started")
    
    


    frame_count = 0   # ✅ FIX
    TARGET_FPS = 20   # ✅ FIX
    FRAME_TIME = 1.0 / TARGET_FPS  # ✅ FIX 
  
    while streaming:
        start_time = time.time()
        #print("STREAMING:", streaming)
        #print("Frame captue thread running...")          
        try:
            frame_count += 1
            
            frame = camera.capture_array("lores")

            if frame_count % 6 == 0:
                results = model.track(frame, persist=True)
                boxes = results[0].boxes
            
            

            

            if boxes.id is not None:
                ids = boxes.id.cpu().numpy().astype(int)
                update_objects(ids)
           
            #print("Frame captured")
            #frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
           
            if len(frame.shape) == 2:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            
            h, w = frame.shape[:2]

            x_offset = min(200, w-1)
            y_offset = min(100, h-1)
            
            roi_width = min(400, w - x_offset)
            roi_height = min(300, h - y_offset)

            roi = frame[y_offset:y_offset + roi_height, x_offset:x_offset + roi_width]
            
            color = (0, 0, 255) if motion_detected else (255, 0, 0)
            
            cv2.rectangle(
                frame,
                (x_offset, y_offset),
                (x_offset + roi_width, y_offset + roi_height),
                (255, 0, 0), #blue box
                2
            )
            gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            small_gray = cv2.resize(gray, (120, 90))
            
            scale_x = roi_width / 160
            scale_y = roi_height / 120
            if prev_frame is None:
                prev_frame = gray
                continue
            
            
            circles = cv2.HoughCircles(
                small_gray,
                cv2.HOUGH_GRADIENT,
                dp=1.5,
                minDist=50,
                param1=120,
                param2 =30,
                minRadius=10,
                maxRadius=50
            )
           
           
            if frame_count % 8 == 0:

                if circles is None:
                    pass  # keep previous bubbles
                else:                          
                   circles = np.uint16(np.around(circles))

                   new_tracked_bubbles = {}
                
                   for (x, y, r) in circles[0, :]:
                  
                    cx = int(x * scale_x) + x_offset
                    cy = int(y * scale_y) + y_offset

                    matched_id = None

                    for bubble_id, (bx, by) in tracked_bubbles.items():
                        distance = np.hypot(cx - bx, cy - by)

                        if distance < 50:
                            matched_id = bubble_id

                            cx = int(0.7 * bx + 0.3 * cx)
                            cy = int(0.7 * by + 0.3 * cy)
                            break

                    if matched_id is None:
                        bubble_id_counter += 1
                        matched_id = bubble_id_counter

                    new_tracked_bubbles[matched_id] = (cx, cy)
                    cv2.circle(
                        frame,
                        (cx, cy), int(r * scale_x)
                        ,
                        (0, 255, 0),
                        2
                    )
                    cv2.circle(
                        frame,
                        (cx, cy),
                        2,
                        (0, 0, 255), 3)
                    

                    for i,(obj_id, data) in enumerate(objects.items()):
                        duration = time.time() - data["first_seen"]
                        cv2.putText(frame, f"ID: {obj_id} ({duration:.1f}s)",
                                    (10, 80 + i * 20),
                                    cv2.FONT_HERSHEY_SIMPLEX,
                                    0.5,
                                    (0, 255, 255),
                                    2)
                        

                    cv2.putText(frame, f"ID: {matched_id}",
                                (cx - 10, cy - 10),
                                cv2.FONT_HERSHEY_SIMPLEX,
                                0.5,
                                (255, 255, 0),
                                2)
                    updated_bubbles = {}

                    for bubble_id, (cx, cy) in new_tracked_bubbles.items():
                        updated_bubbles[bubble_id] = (cx, cy)

                    # keep old ones briefly (fade effect)
                    for bubble_id, (bx, by) in tracked_bubbles.items():
                        if bubble_id not in updated_bubbles:
                            updated_bubbles[bubble_id] = (bx, by)
                   
                    tracked_bubbles = new_tracked_bubbles
                   
               
            frame_delta = cv2.absdiff(prev_frame, gray)
            prev_frame = gray

            #print("Delta mean:", np.mean(frame_delta))

            thresh = cv2.threshold(frame_delta, 15, 255, cv2.THRESH_BINARY)[1]
            thresh = cv2.dilate(thresh, None, iterations=2)

            contours, _ = cv2.findContours(
                thresh,
                cv2.RETR_EXTERNAL,
                cv2.CHAIN_APPROX_SIMPLE
            )

            #print("Contours found:", len(contours))

            motion_detected = any(cv2.contourArea(c) > 1500 for c in contours)

            for contour in contours:
                if cv2.contourArea(contour) <1500:
                    continue
              


                motion_detected = True

                (x, y, w, h) = cv2.boundingRect(contour)
                
                cv2.rectangle(
                    frame,
                    (x_offset, y_offset),
                    (x_offset + roi_width, y_offset + roi_height),
                    (255, 0, 0),
                    2
                )

            if motion_detected:
                motion_timer = time.time()
                print("MOTION!")
                cv2.rectangle(
                    frame,
                    (x_offset, y_offset),
                    (x_offset + roi_width, y_offset + roi_height),
                    (0, 0, 255),
                    2
                )
                cv2.putText(frame, "MOTION DETECTED",
                            (10, 55),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.7,
                            (0, 0, 255),
                            2)
            if motion_recording:
                if time.time() - motion_timer > MOTION_RECORD_SECONDS :
                    print("Stopping motion recording")

                    try:
                        camera.stop_recording(name="main")
                        
                        motion_recording = False
                        recording = False
                        last_record_time = time.time()
                        


                        
                    except Exception as e:
                        print("Error stopping recording:", e)
                        elapsed = time.time() - start_time
                        sleep_time = FRAME_TIME - elapsed
                        if sleep_time > 0:
                            time.sleep(sleep_time)
            if motion_detected and not motion_recording and time.time() - last_record_time > COOLDOWN:
                print("Starting motion recording")
                try:
                    

                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"motion_{timestamp}.mp4"

                    encoder = H264Encoder(bitrate=10000000)
                    file_output = FileOutput(filename) 
                    
                    camera.start_recording(encoder, file_output, name="main")
                    
                    motion_recording = True
                    recording = True    
                    motion_timer = time.time()

                    
                except Exception as e:
                    print("Error starting motion recording:", e)
                    elapsed = time.time() - start_time
                    sleep_time = FRAME_TIME - elapsed
                    if sleep_time > 0:
                            time.sleep(sleep_time)
           
           
            cv2.putText(frame, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        (10, frame.shape[0] - 10),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6,
                        (0, 255, 0
                        ),
                        2)
           
           
            cv2.putText(frame, "CAM 01",(10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
            
            fps = int(1.0 / (time.time() - start_time + 1e-6))

            cv2.putText(frame, f"FPS: {fps}", (frame.shape[1] - 120, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 75]
            ret, jpeg = cv2.imencode('.jpg', frame, encode_param)
            if ret:
                with output.condition:
                    output.frame = jpeg.tobytes()
                    output.condition.notify_all()

            time.sleep(0.03)
                

        
            elapsed = time.time() - start_time
            sleep_time = FRAME_TIME - (time.time() - start_time)
            if sleep_time > 0:
                time.sleep(sleep_time)
        except Exception as e:
            print("ERROR:", e)

def initialize_camera(resolution=(320, 240), fps=20):
    """Initialize the camera."""
    global camera, streaming, output, motion_detected
    

    try:
        logger.info("Initializing camera...")
        camera = Picamera2()
        
        config = camera.create_video_configuration(
            main={"size": (1280, 720)},   
            lores={"size": (640, 480), "format": "RGB888"},
            encode="main"
        )
        camera.configure(config)
        
        
        

        
        camera.start()

        output = StreamingOutput()
        
        

        
        streaming = True
        

        
        # Start frame capture thread
        capture_thread = threading.Thread(target=frame_capture_thread, daemon=True)
        capture_thread.start()
        
        logger.info(f"Camera initialized: {resolution} @ {fps}fps")
        return True
    except Exception as e:
        logger.error(f"Error initializing camera: {e}")
        return False


def cleanup_camera():
    """Clean up camera resources."""
    global camera, streaming
    
    streaming = False
    time.sleep(0.03)
    
    if camera:
        try:
            if recording:
                camera.stop_recording(name="main")
            camera.stop()
            camera.close()
            logger.info("Camera cleaned up")
        except Exception as e:
            logger.error(f"Error cleaning up camera: {e}")
    
    # Clean up temp frame file
    try:
        import os
        if os.path.exists("temp_frame.jpg"):
            os.remove("temp_frame.jpg")
    except:
        pass


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Raspberry Pi 5 Camera Livestream Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES:
  # Start livestream server on localhost:5000
  python3 livestream.py
  
  # Make accessible from network (0.0.0.0 = all interfaces)
  python3 livestream.py --host 0.0.0.0
  
  # Custom port
  python3 livestream.py --port 8080
  
  # Custom resolution
  python3 livestream.py --resolution 1280 720
  
ACCESS FROM BROWSER:
  Local: http://localhost:5000
  Network: http://<raspberry-pi-ip>:5000
        """
    )
    
    parser.add_argument('-p', '--port', type=int, default=5000,
                        help='Port to run server on (default: 5000)')
    parser.add_argument('-H', '--host', type=str, default='127.0.0.1',
                        help='Host to bind to (default: 127.0.0.1)')
    parser.add_argument('-r', '--resolution', type=int, nargs=2, default=[640, 480],
                        metavar=('WIDTH', 'HEIGHT'),
                        help='Camera resolution (default: 640 480)')
    parser.add_argument('-f', '--fps', type=int, default=30,
                        help='Framerate in fps (default: 30)')
    
    args = parser.parse_args()
    
    try:
        if initialize_camera(tuple(args.resolution), args.fps):
            logger.info(f"🚀 Starting livestream server on http://{args.host}:{args.port}")
            app.run(host=args.host, port=args.port, debug=False, threaded=True)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        cleanup_camera()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        cleanup_camera()
