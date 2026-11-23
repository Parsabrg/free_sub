"""Check the public Pages deployment against its locally prepared manifest."""
import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from publishing import verify_live


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('artifact',type=Path)
    parser.add_argument('--attempts',type=int,choices=range(1,7),default=6)
    args=parser.parse_args()
    for attempt in range(args.attempts):
        try:
            manifest=verify_live(args.url,args.artifact)
        except (OSError,ValueError,TimeoutError) as exc:
            if attempt+1==args.attempts:
                raise SystemExit(f'Public snapshot verification failed ({type(exc).__name__})')
            time.sleep(10)
        else:
            print(f'Verified all public files for {manifest["snapshot"][:12]} ({manifest["published"]} entries).')
            return


if __name__=='__main__':
    main()
