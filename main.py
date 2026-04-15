import time
import pyautogui
import cv2
from pyscreeze import Box


TARGET = "bosch.png"

def click_asset(name, image, confidence=0.9, grayscale=True):
    try:
        location = pyautogui.locateOnScreen(
            image, confidence=confidence, minSearchTime=2, grayscale=grayscale
        )
        print(f"[OK] Found {name} at {location}")
        pyautogui.click(location)
        pyautogui.moveTo(10, 10)
    except pyautogui.ImageNotFoundException:
        print(f"[FAIL] Could not find {name}")


def is_onscreen(image, confidence=0.9, grayscale=True):
    try:
        location = pyautogui.locateOnScreen(
            image, confidence=confidence, minSearchTime=2, grayscale=grayscale
        )
        return location
    except pyautogui.ImageNotFoundException:
        return None


def main():
    play_img = cv2.imread("./assets/buttons/play.png")
    dropdown_img = cv2.imread("./assets/menus/dropdown.png")
    dropdown1_img = cv2.imread("./assets/menus/dropdown1.png")
    unofficials_img = cv2.imread("./assets/menus/unofficials.png")
    favorites_img = cv2.imread("./assets/menus/favorites.png")
    favoritetab_img = cv2.imread("./assets/menus/favoriteTab.png")
    scanning_img = cv2.imread("./assets/status/scanning.png")
    target_img = cv2.imread(f"./assets/targets/{TARGET}")
    connect_img = cv2.imread("./assets/buttons/connect.png")
    close_img = cv2.imread("./assets/buttons/close.png")
    refresh_img = cv2.imread("./assets/buttons/refresh.png")
    close2_img = cv2.imread("./assets/buttons/close2.png")
    loading_img = cv2.imread("./assets/status/loading-assets.png")
    time.sleep(2)
    while True:
        if is_onscreen(favorites_img):
            click_asset("refresh", refresh_img)
        click_asset("play", play_img)
        click_asset("dropdown", dropdown_img)
        click_asset("unofficials", unofficials_img)
        while is_onscreen(scanning_img):
            time.sleep(0.2)
        click_asset("dropdown1", dropdown1_img)
        click_asset("favorites", favorites_img)
        while is_onscreen(scanning_img):
            time.sleep(0.2)

        while (
            is_onscreen(favoritetab_img)
            and is_onscreen(target_img, confidence=0.86) is None
        ):
            click_asset("refresh", refresh_img)
            time.sleep(0.5)

        click_asset("target", target_img, confidence=0.86)

        while is_onscreen(favoritetab_img) and is_onscreen(connect_img):
            click_asset("refresh", refresh_img)
            click_asset("target", target_img, confidence=0.86)
            click_asset("connect", connect_img)

        while image := is_onscreen(loading_img):
            time.sleep(0.2)
        if image := (is_onscreen(close_img, confidence=0.5) or is_onscreen(close2_img,confidence=0.7)):
            pyautogui.click(image)
            pyautogui.moveTo(10, 10)


if __name__ == "__main__":
    main()
