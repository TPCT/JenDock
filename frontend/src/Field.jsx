import React, { useId } from "react";
import { Box, TextField as MuiTextField, Typography } from "@mui/material";

// External labels follow the Primer form layout without changing input contracts.
export default function Field({
  label,
  id,
  sx,
  fullWidth,
  className,
  InputLabelProps,
  ...props
}) {
  const generatedId = useId();
  const inputId = id || generatedId;
  if (!label)
    return (
      <MuiTextField
        {...props}
        id={id}
        sx={sx}
        fullWidth={fullWidth}
        className={className}
      />
    );
  return (
    <Box
      className={className}
      sx={{ width: fullWidth ? "100%" : undefined, ...sx }}
    >
      <Typography
        component="label"
        htmlFor={inputId}
        id={InputLabelProps?.id || `${inputId}-label`}
        sx={{
          display: "block",
          fontSize: 13,
          fontWeight: 600,
          mb: 1,
          color: props.error ? "error.main" : "text.primary",
        }}
      >
        {label}
      </Typography>
      <MuiTextField
        {...props}
        id={inputId}
        fullWidth
        size={props.size || "small"}
      />
    </Box>
  );
}
