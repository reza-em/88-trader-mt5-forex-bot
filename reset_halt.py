"""Manual reset of the max-drawdown halt (and daily halt). Run via reset_halt.bat after you've
reviewed what happened. Sets peak equity to the current equity and leaves the bot PAUSED."""
from pathlib import Path

from config import Config, load_dotenv
from state import State

if __name__ == "__main__":
    load_dotenv()
    cfg = Config()
    st = State(cfg.data_dir / "state.json")
    print("halt_drawdown:", st.get("halt_drawdown"), "| halt_daily_day:", st.get("halt_daily_day"))
    ans = input("Reset halts and set the drawdown reference to current equity? Type YES: ").strip()
    if ans == "YES":
        st.update(halt_drawdown=False, halt_daily_day=None, peak_equity=None, paused=True)
        print("Done. The bot stays PAUSED; press ▶️ in Telegram when ready. / انجام شد؛ ربات متوقف است.")
    else:
        print("Nothing changed.")
