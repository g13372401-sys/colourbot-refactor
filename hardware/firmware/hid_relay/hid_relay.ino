/*
 * hid_relay.ino -- the "arduino" input backend's other half.
 * ==========================================================
 *
 * Flash this onto any board whose USB stack can be a HID device *and* a serial
 * port at the same time - Arduino Leonardo / Micro / Pro Micro (ATmega32u4),
 * Teensy, or an RP2040 board with the Arduino core.  A classic Uno/Nano will
 * NOT work: its USB chip cannot present a HID interface.
 *
 * The PC sends one short ASCII line per action on the serial port; the board
 * replays it on its HID interfaces.  Windows sees a normal USB mouse and a
 * normal USB keyboard, so the resulting events are hardware events:
 *
 *     MSLLHOOKSTRUCT.flags  == 0   (no LLMHF_INJECTED, no LLMHF_LOWER_IL_INJECTED)
 *     KBDLLHOOKSTRUCT.flags == 0   (no LLKHF_INJECTED)
 *
 * Protocol (see inputbackends/arduino.py, which is the only speaker):
 *
 *     P            ping                        -> "HID1"
 *     M dx dy      relative mouse move         -> "K"
 *     D b          mouse button down (1/2/3)   -> "K"
 *     U b          mouse button up   (1/2/3)   -> "K"
 *     W n          wheel, n clicks             -> "K"
 *     K c          key down, Arduino key code  -> "K"
 *     R c          key up,   Arduino key code  -> "K"
 *     X            release everything          -> "K"
 *
 * Every command is answered, so the host stays in lock step with the board and
 * never runs ahead of the USB polling interval.
 */

#include <Mouse.h>
#include <Keyboard.h>

static const uint8_t BUTTONS[] = {0, MOUSE_LEFT, MOUSE_RIGHT, MOUSE_MIDDLE};

void setup() {
  Serial.begin(115200);
  Mouse.begin();
  Keyboard.begin();
}

void loop() {
  if (!Serial.available()) return;

  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.length() == 0) return;

  const char op = line.charAt(0);
  const int  sp = line.indexOf(' ');
  long a = 0, b = 0;
  if (sp > 0) {
    const int sp2 = line.indexOf(' ', sp + 1);
    a = line.substring(sp + 1, sp2 > 0 ? sp2 : line.length()).toInt();
    if (sp2 > 0) b = line.substring(sp2 + 1).toInt();
  }

  switch (op) {
    case 'P': Serial.println("HID1");                       return;
    case 'M': Mouse.move((signed char)a, (signed char)b, 0); break;
    case 'D': if (a >= 1 && a <= 3) Mouse.press(BUTTONS[a]); break;
    case 'U': if (a >= 1 && a <= 3) Mouse.release(BUTTONS[a]); break;
    case 'W': Mouse.move(0, 0, (signed char)a);             break;
    case 'K': Keyboard.press((uint8_t)a);                   break;
    case 'R': Keyboard.release((uint8_t)a);                 break;
    case 'X': Keyboard.releaseAll(); Mouse.release(MOUSE_LEFT);
              Mouse.release(MOUSE_RIGHT); Mouse.release(MOUSE_MIDDLE); break;
    default:  Serial.println("?");                          return;
  }
  Serial.println("K");
}
