#!/usr/bin/env python3
"""
Raspberry Pi 5 Camera Livestream Server

"""



import io
import threading
import logging
import time
import numpy as np
import cv2

from picamera2.outputs import FileOutput

from datetime import datetime
from pathlib import Path
from flask import Flask, render_template_string, Response, jsonify
from PIL import Image

# --- CONFIG ---

COOLDOWN = 2
output = None





#motion_detected = False
 
TARGET_FPS = 20
FRAME_TIME = 1.0 / TARGET_FPS  # Limit to TARGET_FPS  

ROI = (320, 120, 480, 360)
CIRCLE_EVERY_N_FRAMES = 2






# --- GLOBALS ---

tracked_bubbles = {}
bubble_counts = {}
previous_positions = {}
MIN_CONFIRM_FRAMES = 2

bubble_id_counter = 0
last_record_time = 0

motion_timer = 0
MOTION_RECORD_SECONDS = 3

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

            img.onerror = () => {
                showError("Stream disconnected");
            };
            const now = new Date().getTime();
            img.src = '/video_feed?t=' + now;
            streamTimeout = setTimeout(refreshStream, 80);
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
        window.addEventListener('load', () => {
            document.getElementById('stream').src = '/video_feed';
        });
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



def match_bubbles(detected, previous, max_dist=50):
    """Match new detections to existing tracked bubbles."""
    global bubble_id_counter

    updated = {}

    for (cx, cy, r, ) in detected:
         best_id = None
         best_dist = max_dist

         for bubble_id, (px, py, _, _) in previous.items():
             dist = np.hypot(cx - px, cy - py)
             if dist < best_dist:
                 best_dist = dist
                 best_id = bubble_id_counter
        
         if best_id is None:
            bubble_id_counter += 1
            best_id = bubble_id_counter
        
          # ⚡ ADD TIME HERE
         updated[best_id] = (cx, cy, r, time.time()) 
         
    return updated

def detect_bubbles(small_gray, scale_x, scale_y, x_offset, y_offset):
    small_gray = cv2.GaussianBlur(small_gray, (5, 5), 0)
    """Detect circles and filter noise."""
    circles = cv2.HoughCircles(
        small_gray,
        cv2.HOUGH_GRADIENT,
        dp=2.0,
        minDist=50,
        param1=80,
        param2 =20,
        minRadius=6,
        maxRadius=20
    )

    results = []

    if circles is None:
        return results
    
    circles = np.uint16(np.around(circles))
    


    for (x, y, r) in circles[0]:
        if y >= small_gray.shape[0] or x >= small_gray.shape[1]:
            continue

        if small_gray[y, x] < 150:
            continue

        cx = int(x * scale_x) + x_offset
        cy = int(y * scale_y) + y_offset

        results.append((cx, cy, r,))

    return results

    
def frame_capture_thread():
    """Continuously capture frames from camera."""
    global prev_frame,  motion_detected,  tracked_bubbles, ROI, ROI_LOCKED
    
    last_boxes =[]
    
    
   
   

    
    frame_count = 0
    FRAME_TIME = 1.0 / 30  # Limit to 30 FPS

    boxes = None # safe init
    
    print("THREAD STARTED")
    logger.info("Frame capture thread started")
    
    
    last_detect_time = 0

    
    while streaming:
        start_time = time.time()
        #print("STREAMING:", streaming)
        #print("Frame captue thread running...")          
        try:
            frame_count += 1
            
            request_obj = camera.capture_request()

            frame = camera.capture_array("main")   # HD
            lores = camera.capture_array("lores")  # Low-res
            
            request_obj.release()
      
            
            frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            gray = cv2.cvtColor(lores, cv2.COLOR_RGB2GRAY)
            small = cv2.resize(gray, (64, 48))
            
            boxes = None
            
            if frame_count % 2 == 0:
                
                continue
           
          
             # --- ROI ---
            h, w = frame.shape[:2]
            
            x1, y1, x2, y2 = ROI

            x1 = max(0, x1)
            y1 = max(0, y1)
            x2 = min(w, x2)
            y2 = min(h, y2)
            
            # Validate BEFORE crop
            if x2 <= x1 or y2 <= y1:
                print("Invalid ROI coordinates, skipping frame")
                continue

            roi = frame[y1:y2, x1:x2]

            if roi.size == 0:
                print("Empty ROI, skipping frame")
                continue
            
            if roi.shape[0] < 20 or roi.shape[1] < 20:
                print("ROI too small, skipping frame")
                continue
            
            roi_h, roi_w = roi.shape[:2]
            x_offset, y_offset = x1, y1

            cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 0, 0), 2)
            cv2.putText(frame, "ROI Active", (x1, y1 - 5), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1)
            
            gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY) 
            small = cv2.resize(gray, (64, 48), interpolation=cv2.INTER_AREA)
           
            # --- Bubble detection ---
            detections = []
       
           

           
                
            if time.time() - last_detect_time > 0.15:

                detections = detect_bubbles(
                    small_gray=small,
                    scale_x=roi_w/64, 
                    scale_y=roi_h/48, 
                    x_offset=x_offset, 
                    y_offset=y_offset
                ) 
                
                last_detect_time = time.time()
                
                # Filters

                detections = [(cx, cy, r) for (cx, cy, r) in detections if 10 < r < 20]

                pipe_center_x = x_offset + (roi_w // 2)

                cv2.line(frame, 
                         (pipe_center_x, y1), 
                         (pipe_center_x, y2), 
                         (255, 255, 0), 1)
                
                detections = [(cx, cy, r) for (cx, cy, r) in detections if abs(cx - pipe_center_x) < 40]

                for (cx, cy, r) in detections:
                    cv2.circle(frame, (cx, cy), int(r), (0, 255, 255), 2)
            
            if time.time() - last_detect_time > 0.15:
                detections = detect_bubbles(
                    small_gray=small,
                    scale_x=roi_w/64, 
                    scale_y=roi_h/48, 
                    x_offset=x_offset, 
                    y_offset=y_offset
                )
                last_detect_time = time.time()
                matched = match_bubbles(detections, tracked_bubbles)
                
                for (cx, cy, r) in detections:
                    cv2.circle(frame, (cx, cy), r, (0, 255, 255), 1)
                
                # Tracking

                now = time.time()
                updated_bubbles = {}

                for bid, (cx, cy, r, t) in matched.items():
                    
                    
                    # Smooth movement (optional but good)
                    if bid in tracked_bubbles:
                        old_x, old_y, _, _= tracked_bubbles[bid]

                        cx = int(0.7 * old_x + 0.3 * cx)
                        cy = int(0.7 * old_y + 0.3 * cy)
                    
                    #Update count
                    bubble_counts[bid] = bubble_counts.get(bid, 0) + 1

                    #Only accept stable bubbles
                    if bubble_counts[bid] >= MIN_CONFIRM_FRAMES:
                         updated_bubbles[bid] = (cx, cy, r, now)

                tracked_bubbles = updated_bubbles
                

           
            for bid in list(tracked_bubbles.keys()):
                _, _, _, t = tracked_bubbles[bid]

                if now - t > 2.0:
                    del tracked_bubbles[bid]
                    bubble_counts.pop(bid, None)
            
            # Only keep upward movement
            filtered_bubbles={}

            for bid, (cx, cy, r, t) in tracked_bubbles.items():
                
                
                if bid in previous_positions:
                    

                    #keep only upward movement
                    if cy < previous_positions[bid]: #moving up
                        filtered_bubbles[bid] = (cx, cy, r, t)
                
                previous_positions[bid] = cy
            
            tracked_bubbles = filtered_bubbles
               
            # --- Draw bubbles ---  
            
            for bid, (cx, cy, r, now) in tracked_bubbles.items():
                cv2.circle(frame, (cx, cy), 10, (0, 255, 0), 2)
                
               

        
                if prev_frame is None:
                    prev_frame = gray
                    continue         
            

            # --- Motion detection ---
            #if prev_frame is None or gray.shape != prev_frame.shape:
                #prev_frame = gray.copy()
                #continue
            
            #delta = cv2.absdiff(prev_frame, gray)
            
            #prev_frame = gray

            #thresh = cv2.threshold(delta, 15, 255, cv2.THRESH_BINARY)[1]
            #thresh = cv2.dilate(thresh, None, iterations=2)

            #contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

           #motion_detected = any(cv2.contourArea(c) > 1500 for c in contours)

            # --- Overlay ---
            
            
            # Frame counter (TOP-LEFT)
            
            y = 30
            
            cv2.rectangle(frame, (5, 5), (180, 100), (0,0,0), -1)
            cv2.putText(frame, f"Frame: {frame_count}", (10, y), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255, 0), 2)
           
            # FPS (TOP-LEFT)
            fps = min(30, 1.0 / (time.time() - start_time))
            
            y +=30
            
            cv2.rectangle(frame, (5, 5), (180, 100), (0,0,0), -1)
            cv2.putText(frame, f"FPS: {fps:.1f}", (10, y), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255, 0), 2)
            
            # Timestamp (TOP-LEFT)
           
            y +=30

            cv2.rectangle(frame, (5, 5), (180, 100), (0,0,0), -1)
            cv2.putText(frame, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
                        (10, y), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255, 0), 2)
            
            #--- Overlay ---

            overlay = frame.copy()

            box_width = 220
            box_height = 80

            x1 = frame.shape[1] - box_width - 10
            y1 = 10
            x2 = frame.shape[1] - 10
            y2 = 10 + box_height

            cv2.rectangle(overlay, (x1, y1), (x2, y2), (0, 0, 0), -1)

            alpha = 0.5
            frame = cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0)

            y = y1 + 25
            line_height = 25

            texts = [
                f"FPS: {fps:.1f}",
                f"Frame: {frame_count}",
                f"Bubbles: {len(tracked_bubbles)}",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ]
            
            for text in texts:
                cv2.putText(frame, text, (x1 + 10, y), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255, 0), 2)
                y += line_height

            # --- Encode and stream ---
            ret, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 40])
            if ret:
                with output.condition:
                    output.frame = jpeg.tobytes()
                    output.condition.notify_all()

            # ---FPS Limiter---
            elapsed = time.time() - start_time
            if elapsed < FRAME_TIME:
                time.sleep(FRAME_TIME - elapsed)
        except Exception as e:
            print("Thread error:", e)

def initialize_camera(resolution=(960, 540), fps=30):
    """Initialize the camera."""
    global camera, streaming, output
    

    try:
        logger.info("Initializing camera...")
        camera = Picamera2()
        
        config = camera.create_video_configuration(
            main={
                "size": resolution,   
            },
            lores={
                "size": (320, 240), 
                "format": "RGB888"
            },
            controls={
                "FrameRate": fps,
            },
            buffer_count=2
            
           
        )
        camera.configure(config)
        
        
        

        
        camera.start()

        output = StreamingOutput()
        
        

        
        streaming = True
        
        time.sleep(0.5)  # Allow camera to warm up
        
        # Start frame capture thread
        capture_thread = threading.Thread(
            target=frame_capture_thread, 
            daemon=True
            )
        
        capture_thread.start()
        
        logger.info(f"Camera initialized: {resolution} @ {fps}fps")
        return True
    
    except Exception as e:
        logger.error(f"Error initializing camera: {e}")
        return False


def cleanup_camera():
    """Clean up camera resources."""
    global camera, streaming, recording
    
    streaming = False
    
    # Give thread time to exit
    time.sleep(0.2)
    
    if camera:
        try:
            if recording:
                try:
                    camera.stop_recording()
                except Exception:
                    pass
                recording = False
           
            camera.stop()
            camera.close()
            camera = None

            logger.info("Camera cleaned up")
        
        except Exception as e:
            logger.error(f"Error cleaning up camera: {e}")
    
    # Clean up temp frame file
    try:
        import os
        if os.path.exists("temp_frame.jpg"):
            os.remove("temp_frame.jpg")
    except Exception:
        pass



if __name__ == '__main__':
    import argparse
    import atexit
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
  python3 livestream.py --resolution 960 540
  
ACCESS FROM BROWSER:
  Local: http://localhost:5000
  Network: http://<raspberry-pi-ip>:5000
        """
    )
    
    parser.add_argument('-p', '--port', type=int, default=5000)
                      
    parser.add_argument('-H', '--host', type=str, default='127.0.0.1')
                       
    parser.add_argument('-r', '--resolution', type=int, nargs=2, default=[960, 540])
                       
    parser.add_argument('-f', '--fps', type=int, default=30)
                       
    
    args = parser.parse_args()

    # ✅ ALWAYS cleanup on exit
    atexit.register(cleanup_camera)
    
    try:
        if initialize_camera(tuple(args.resolution), args.fps):
            logger.info(f"🚀 Starting livestream server on http://{args.host}:{args.port}")
            
            app.run(
                host=args.host, 
                port=args.port, 
                debug=False, 
                threaded=True,
                use_reloader=False # ✅ ALWAYS cleanup on exit
            )
    
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        
    except Exception as e:
        logger.error(f"Fatal error: {e}")
    finally:
        #✅ GUARANTEED cleanup   
        cleanup_camera()


