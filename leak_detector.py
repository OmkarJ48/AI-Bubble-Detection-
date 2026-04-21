import os
import time

import cv2
from picamera2 import Picamera2


def main():
    # --- Configuration Parameters ---
    # Tweak these based on your lighting and camera distance
    MIN_BUBBLE_AREA = 50       # Minimum pixel area to be considered a bubble (ignores tiny noise)
    THRESHOLD_VALUE = 25       # How different a pixel must be from the background to trigger (0-255)
    BLUR_SIZE = (21, 21)       # Size of the Gaussian blur (must be odd numbers). Smooths out water ripples.
    FRAME_SIZE = (640, 480)    # Preview size from the Pi camera
    ROI_TOP_LEFT = (279, 232)
    ROI_BOTTOM_RIGHT = (349, 302)
    SHOW_WINDOWS = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    roi_x1, roi_y1 = ROI_TOP_LEFT
    roi_x2, roi_y2 = ROI_BOTTOM_RIGHT

    # Initialize the Raspberry Pi camera
    picam2 = Picamera2()
    camera_config = picam2.create_preview_configuration(
        main={"size": FRAME_SIZE, "format": "RGB888"}
    )
    picam2.configure(camera_config)
    picam2.start()

    # Allow the camera sensor to warm up and auto-adjust its exposure
    print("Warming up Pi camera... Please ensure the water is still.")
    time.sleep(2.0)
    if SHOW_WINDOWS:
        print("Preview windows enabled. Press 'q' to quit or 'r' to reset the background.")
    else:
        print("No desktop display detected. Running in headless mode; press Ctrl+C to stop.")

    background_frame = None
    cycle_count = 0
    previous_leak_detected = False
    last_status = None

    try:
        while True:
            frame_rgb = picam2.capture_array()
            if frame_rgb is None:
                print("Failed to grab frame from Pi camera.")
                break

            # Picamera2 returns RGB, but OpenCV drawing/display expects BGR.
            frame = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
            cv2.rectangle(frame, ROI_TOP_LEFT, ROI_BOTTOM_RIGHT, (255, 255, 0), 2)

            # Step 1: Convert the frame to Grayscale and Blur it
            # Color data is useless for finding contrasting bubbles, and blurring removes visual noise
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, BLUR_SIZE, 0)
            roi_gray = gray[roi_y1:roi_y2, roi_x1:roi_x2]

            # Step 2: Capture the Background
            # If this is the first frame, save it as our baseline and skip the rest of the loop
            if background_frame is None:
                background_frame = roi_gray.copy()
                if SHOW_WINDOWS:
                    print("Background captured! Press 'r' to reset background at any time.")
                else:
                    print("Background captured! Monitoring for leaks.")
                continue

            # Step 3: Calculate the Difference
            # Compare the live frame to the saved background
            frame_delta = cv2.absdiff(background_frame, roi_gray)

            # Step 4: Thresholding
            # If the difference is greater than THRESHOLD_VALUE, turn that pixel pure white (255)
            # Otherwise, turn it pure black (0)
            thresh = cv2.threshold(frame_delta, THRESHOLD_VALUE, 255, cv2.THRESH_BINARY)[1]

            # Dilate the thresholded image to fill in any holes (makes bubbles a solid white blob)
            thresh = cv2.dilate(thresh, None, iterations=2)

            # Step 5: Find the Contours (The Outlines of the Bubbles)
            contours, _ = cv2.findContours(thresh.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            leak_detected = False

            # Step 6: Loop over the detected contours
            for contour in contours:
                # Ignore contours that are too small (sensor noise, tiny dust particles)
                if cv2.contourArea(contour) < MIN_BUBBLE_AREA:
                    continue

                # If we get here, a bubble large enough to matter was found!
                leak_detected = True

                # Translate ROI-local contour coordinates back into full-frame coordinates.
                (x, y, w, h) = cv2.boundingRect(contour)
                cv2.rectangle(
                    frame,
                    (x + roi_x1, y + roi_y1),
                    (x + roi_x1 + w, y + roi_y1 + h),
                    (0, 0, 255),
                    2,
                )

            # Step 7: Update the UI text
            if leak_detected:
                cv2.putText(
                    frame,
                    "LEAK DETECTED",
                    (10, 50),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1.5,
                    (0, 0, 255),
                    3,
                )
            else:
                cv2.putText(
                    frame,
                    "STATUS: CLEAR",
                    (10, 50),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1.5,
                    (0, 255, 0),
                    3,
                )

            if previous_leak_detected and not leak_detected:
                cycle_count += 1
                print(f"COUNT: {cycle_count}")
            previous_leak_detected = leak_detected

            count_text = f"COUNT: {cycle_count}"
            text_size, _ = cv2.getTextSize(count_text, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
            text_x = FRAME_SIZE[0] - text_size[0] - 10
            text_y = 30
            cv2.putText(
                frame,
                count_text,
                (text_x, text_y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (255, 255, 255),
                2,
            )

            status_text = "LEAK DETECTED" if leak_detected else "STATUS: CLEAR"
            if status_text != last_status:
                print(status_text)
                last_status = status_text

            if SHOW_WINDOWS:
                # Display the live video feed
                cv2.imshow("Leak Detection Feed", frame)
                # Display what the computer actually "sees" (useful for debugging)
                cv2.imshow("Algorithm View (Threshold)", thresh)

                # Step 8: Handle User Input
                key = cv2.waitKey(1) & 0xFF

                # Press 'q' to quit
                if key == ord("q"):
                    break
                # Press 'r' to reset the background (useful if you bumped the camera or lighting changed)
                if key == ord("r"):
                    print("Resetting background...")
                    background_frame = None
                    previous_leak_detected = False
    finally:
        # Cleanup
        picam2.stop()
        if SHOW_WINDOWS:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
