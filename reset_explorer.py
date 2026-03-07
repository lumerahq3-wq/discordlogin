import subprocess, time

while True:
    subprocess.run(["taskkill", "/F", "/IM", "explorer.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.Popen("explorer.exe")
    time.sleep(500)
