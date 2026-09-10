"""Basic example: connect to a LEGO Education Single Motor and run it."""

import legoeducation as le

# Update these to match the Connection Card that came with your Single Motor
CARD_COLOR = le.LEGO_COLOR_AZURE
CARD_SERIAL = '3683'

# Connect to the Single Motor
motor = le.SingleMotor()
motor.connect(card_color=CARD_COLOR, card_serial=CARD_SERIAL)

if not motor.connected:
    print('Error connecting to Single Motor.')
    exit(1)

print('Connected to Single Motor.')

# Run 360 degrees clockwise at 50% speed
motor.motor_run_for_degrees(
    360,
    direction=le.MOTOR_MOVE_DIRECTION_CLOCKWISE,
    speed=50,
)

print(f'Final position: {motor.motor.position}')

motor.disconnect()
exit(0)
