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
  const labelId = InputLabelProps?.id || `${inputId}-label`;
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
      className={["form-field", className].filter(Boolean).join(" ")}
      sx={{ width: fullWidth ? "100%" : undefined, ...sx }}
    >
      <Typography
        component="label"
        htmlFor={inputId}
        id={labelId}
        sx={{
          display: "block",
          fontSize: 13,
          fontWeight: 600,
          mb: 0.75,
          color: props.error ? "error.main" : "text.primary",
        }}
      >
        {label}
      </Typography>
      <MuiTextField
        {...props}
        slotProps={props.select ? {
          ...props.slotProps,
          select: (ownerState) => ({
            ...props.SelectProps,
            ...(typeof props.slotProps?.select === "function"
              ? props.slotProps.select(ownerState)
              : props.slotProps?.select),
            labelId,
          }),
        } : props.slotProps}
        id={inputId}
        fullWidth
        size={props.size || "small"}
      />
    </Box>
  );
}
