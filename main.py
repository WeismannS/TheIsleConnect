import time
from pathlib import Path

import cv2
import pyautogui


ASSET_DIR = Path(__file__).resolve().parent / "assets"
TARGET = "bosch.png"
POLL_SECONDS = 0.2
REFRESH_PAUSE_SECONDS = 0.5


def load_asset(*parts):
    path = ASSET_DIR.joinpath(*parts)
    image = cv2.imread(str(path))
    if image is None:
        raise FileNotFoundError(f"Could not load image asset: {path}")
    return image


def click_match(location):
    point = pyautogui.center(location)
    pyautogui.click(point.x, point.y)
    pyautogui.moveTo(10, 10)


def click_asset(name, image, confidence=0.9, grayscale=True):
    location = is_onscreen(image, confidence=confidence, grayscale=grayscale)
    if location is None:
        print(f"[FAIL] Could not find {name}")
        return False

    print(f"[OK] Found {name} at {location}")
    click_match(location)
    return True


def is_onscreen(image, confidence=0.9, grayscale=True):
    try:
        location = pyautogui.locateOnScreen(
            image, confidence=confidence, minSearchTime=2, grayscale=grayscale
        )
        return location
    except pyautogui.ImageNotFoundException:
        return None


def wait_until_hidden(image):
    while is_onscreen(image):
        time.sleep(POLL_SECONDS)


def main():
    play_img = load_asset("buttons", "play.png")
    dropdown_img = load_asset("menus", "dropdown.png")
    dropdown1_img = load_asset("menus", "dropdown1.png")
    unofficials_img = load_asset("menus", "unofficials.png")
    favorites_img = load_asset("menus", "favorites.png")
    favoritetab_img = load_asset("menus", "favoriteTab.png")
    scanning_img = load_asset("status", "scanning.png")
    target_img = load_asset("targets", TARGET)
    connect_img = load_asset("buttons", "connect.png")
    close_img = load_asset("buttons", "close.png")
    refresh_img = load_asset("buttons", "refresh.png")
    close2_img = load_asset("buttons", "close2.png")
    loading_img = load_asset("status", "loading-assets.png")
    time.sleep(2)
    while True:
        if is_onscreen(favorites_img):
            click_asset("refresh", refresh_img)
        click_asset("play", play_img)
        click_asset("dropdown", dropdown_img)
        click_asset("unofficials", unofficials_img)
        wait_until_hidden(scanning_img)
        click_asset("dropdown1", dropdown1_img)
        click_asset("favorites", favorites_img)
        wait_until_hidden(scanning_img)

        while (
            is_onscreen(favoritetab_img)
            and is_onscreen(target_img, confidence=0.86) is None
        ):
            click_asset("refresh", refresh_img)
            wait_until_hidden(scanning_img)
            time.sleep(REFRESH_PAUSE_SECONDS)

        click_asset("target", target_img, confidence=0.86)

        while is_onscreen(favoritetab_img) and is_onscreen(connect_img):
            click_asset("refresh", refresh_img)
            wait_until_hidden(scanning_img)
            if click_asset("target", target_img, confidence=0.86):
                click_asset("connect", connect_img)
            else:
                time.sleep(REFRESH_PAUSE_SECONDS)

        while image := is_onscreen(loading_img):
            time.sleep(POLL_SECONDS)
        if image := (
            is_onscreen(close_img, confidence=0.5)
            or is_onscreen(close2_img, confidence=0.7)
        ):
            click_match(image)


if __name__ == "__main__":
    main()
