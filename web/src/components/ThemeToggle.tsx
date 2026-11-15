// Light/dark switch, shared by the landing page and the console's top bar.
import { Moon, Sun } from "lucide-react";
import { useTheme } from "../lib/theme";

export function ThemeToggle() {
  const [theme, toggle] = useTheme();
  const next = theme === "dark" ? "light" : "dark";
  return (
    <button className="iconbtn themebtn" onClick={toggle} aria-label={`Switch to ${next} theme`}>
      <span className="themeicon" key={theme}>
        {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
      </span>
      {theme === "dark" ? "Light" : "Dark"}
    </button>
  );
}
