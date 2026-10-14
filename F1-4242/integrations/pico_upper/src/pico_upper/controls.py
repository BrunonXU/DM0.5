"""Upper-only menu handler: neither recording nor reset nor body/base gestures."""
import threading

def start_menu_worker(controller):
    def loop():
        while not controller._stop_event.is_set():
            try:
                pressed = bool(controller.xr_client.get_button_state_by_name('left_menu_button'))
            except Exception:
                pressed = False
            if pressed and not controller._local_frame_button_pressed:
                with controller._local_frame_lock:
                    controller._local_frame_generation += 1
                print('UPPER_LOCAL_FRAME_RECENTER_REQUEST', flush=True)
            controller._local_frame_button_pressed = pressed
            controller._stop_event.wait(.005)
    thread = threading.Thread(target=loop, name='UpperMenu', daemon=True)
    thread.start()
    return thread
