import cv2
import time

def main():
    # --- Configuration Parameters ---
    # Tweak these based on your lighting and camera distance
    MIN_BUBBLE_AREA = 50       # Minimum pixel area to be considered a bubble (ignores tiny noise)
    THRESHOLD_VALUE = 25       # How different a pixel must be from the background to trigger (0-255)
    BLUR_SIZE = (21, 21)       # Size of the Gaussian blur (must be odd numbers). Smooths out water ripples.
    
    # Initialize the USB Webcam (0 is usually the default built-in/USB camera)
    cap = cv2.VideoCapture(0)

    # Allow the camera sensor to warm up and auto-adjust its exposure
    print("Warming up camera... Please ensure the water is still.")
    time.sleep(2.0)

    background_frame = None

    while True:
        ret, frame = cap.read()
        if not ret:
            print("Failed to grab frame. Is the webcam connected?")
            break

        # Step 1: Convert the frame to Grayscale and Blur it
        # Color data is useless for finding contrasting bubbles, and blurring removes visual noise
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, BLUR_SIZE, 0)

        # Step 2: Capture the Background
        # If this is the first frame, save it as our baseline and skip the rest of the loop
        if background_frame is None:
            background_frame = gray
            print("Background captured! Press 'r' to reset background at any time.")
            continue

        # Step 3: Calculate the Difference
        # Compare the live frame to the saved background
        frame_delta = cv2.absdiff(background_frame, gray)

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

            # Calculate the bounding box for the bubble and draw it on the original color frame
            (x, y, w, h) = cv2.boundingRect(contour)
            cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 0, 255), 2) # Red box

        # Step 7: Update the UI text
        if leak_detected:
            cv2.putText(frame, "LEAK DETECTED", (10, 50), 
                        cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 255), 3)
        else:
            cv2.putText(frame, "STATUS: CLEAR", (10, 50), 
                        cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 255, 0), 3)

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
        elif key == ord("r"):
            print("Resetting background...")
            background_frame = None

    # Cleanup
    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()