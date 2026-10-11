import React, { useState } from "react";
import { IconButton, Tooltip } from "@mui/material";
import { Info } from "./icons";

export default function Hint({ label, children }) {
  const [open, setOpen] = useState(false);
  return (
    <Tooltip
      open={open}
      onOpen={() => setOpen(true)}
      onClose={() => setOpen(false)}
      title={children}
      describeChild
      enterTouchDelay={0}
      leaveTouchDelay={10000}
    >
      <IconButton
        size="small"
        aria-label={label}
        onKeyDownCapture={event => {
          if (event.key === "Escape" && open) {
            event.stopPropagation();
            setOpen(false);
          }
        }}
        sx={{
          color: "text.secondary",
          verticalAlign: "middle",
          width: { xs: 40, sm: 32 },
          minWidth: { xs: 40, sm: 32 },
          height: { xs: 40, sm: 32 },
          minHeight: { xs: 40, sm: 32 },
          flexShrink: 0,
        }}
      >
        <Info sx={{ fontSize: 16 }} />
      </IconButton>
    </Tooltip>
  );
}
