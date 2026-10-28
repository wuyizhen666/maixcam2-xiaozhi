"""Run every independent stop cleanup, even if another recovery action fails."""
import json
from hardware_tools import force_light_off
from peripheral_tools import restore_user_led


def main():
    results = {}
    failed = False
    for name, action in [('illumination', force_light_off), ('user_led', restore_user_led)]:
        try:
            results[name] = {'ok': True, 'result': action()}
        except Exception as exc:
            failed = True
            results[name] = {'ok': False, 'error': str(exc)}
    print(json.dumps(results, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
