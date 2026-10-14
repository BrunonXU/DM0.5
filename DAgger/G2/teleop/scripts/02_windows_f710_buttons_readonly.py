#!/usr/bin/env python3
"""Standalone pygame button probe: no project imports, network or GDK."""
import argparse
import os
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int, default=0)
    args = parser.parse_args()
    os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
    os.environ.setdefault("SDL_JOYSTICK_THREAD", "1")
    import pygame

    pygame.init()
    pygame.joystick.init()
    try:
        print("RAW BUTTON PROBE v1 - READ ONLY - no network, no robot", flush=True)
        print(f"Script: {os.path.abspath(__file__)}", flush=True)
        if not 0 <= args.index < pygame.joystick.get_count():
            print("F710 not found. Check receiver, X mode and joy.cpl; then rerun.")
            return 2
        joystick = pygame.joystick.Joystick(args.index)
        joystick.init()
        device_id = joystick.get_instance_id()
        print(f"Device: {joystick.get_name()}, buttons={joystick.get_numbuttons()}", flush=True)
        print("Press only START, hold 1 second, release; repeat twice. Ctrl-C exits.", flush=True)
        print("Expected START index is 7, UNVERIFIED until your physical test.", flush=True)
        previous = None
        while joystick.get_init():
            for event in pygame.event.get():
                if event.type == pygame.JOYDEVICEREMOVED and event.instance_id == device_id:
                    print("Device disconnected; rerun after reconnecting.", flush=True)
                    return 2
                if event.type in (pygame.JOYBUTTONDOWN, pygame.JOYBUTTONUP) and event.instance_id == device_id:
                    edge = "DOWN" if event.type == pygame.JOYBUTTONDOWN else "UP"
                    print(f"button={event.button} {edge} host_monotonic_ns={time.monotonic_ns()}", flush=True)
            pressed = tuple(i for i in range(joystick.get_numbuttons()) if joystick.get_button(i))
            if pressed != previous:
                print(f"pressed_buttons={list(pressed)}", flush=True)
                previous = pressed
            time.sleep(.02)
    except KeyboardInterrupt:
        print("Stopped by operator.")
    finally:
        pygame.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
