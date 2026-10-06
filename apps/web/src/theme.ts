import { type Theme, webLightTheme } from "@fluentui/react-components"

// Public app theme: Fluent keeps ownership of accent, focus, and interaction states.
export const appTheme: Theme = {
  ...webLightTheme,
  fontFamilyBase: 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
  borderRadiusMedium: "6px",
  borderRadiusLarge: "10px",
  borderRadiusXLarge: "12px",
  colorNeutralBackground1: "#ffffff",
  colorNeutralBackground2: "#f3f6fa",
  colorNeutralBackground3: "#eaf0f6",
  colorNeutralForeground1: "#202c3a",
  colorNeutralForeground2: "#445469",
  colorNeutralForeground3: "#526278",
  colorNeutralForeground4: "#526278",
  colorNeutralStroke1: "#ced7e2",
  colorNeutralStroke2: "#dce3ec",
  colorNeutralStrokeAccessible: "#74849a",
}
