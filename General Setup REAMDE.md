Switch environments in the current repsitory and successful execution of our script :
1. In the root directory :
''''bash
sudo apt update
sudo apt install -y python3-libcamera python3-picamera2

1. Login into Raspberry Pi SSH running on 10.1.6.70
''''bash
ssh pi@10.1.6.70/ su pi
               
            OR

1. Login into the root of the current environment :
Just be in that location, you are already there         
           

2. Go to project location :
''''bash
cd /home/pi/Documents/RnD_Camera/RnD_Camera
            
            OR

2. Go to project location inside pi :
''''bash
cd /home/pi/Documents/RnD_Camera/RnD_Camera

3. Activate the virtual environment : 
''''bash
source venv/bin/activate

            OR
''''bash
source venv/bin/activate

            OR

3. Activate the virtual environment inside pi :
''''bash
source venv/bin/activate

4. Upgrade to the latest python version
''''bash
python -m pip install --upgrade pip

5. Install Flask via pip 
pip install Flask Werkzeug pillow

6. Check if the Python correctly imports and prints libcamera, picamera2 and cv2/numpy
python3 -c "import libcamera; print('libcamera ok')"
python3 -c "import picamera2; print('picamera2 ok')"
python3 -c "import cv2, numpy; print('cv2/numpy ok')"

7. Running the script:
''''bash
python your_script.py

8. Deactivate the virtual environment
'''bash
deactivate

9. Exiting the virtual environment :
''''bash
deactivate

