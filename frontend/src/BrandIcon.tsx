import { useState } from "react";
import { Cpu } from "@phosphor-icons/react";
import { modelBrandName, providerIconPath } from "./models.types";

export type BrandIconProps = {
  brand?: string | null;
  size?: number;
  className?: string;
  label?: string;
  decorative?: boolean;
};
export function BrandIcon({
  brand,
  size = 24,
  className = "",
  label,
  decorative = false,
}: BrandIconProps) {
  const source = providerIconPath(brand);
  const [failedSource, setFailedSource] = useState<string | null>(null);
  const name = label ?? modelBrandName(brand);
  return source && failedSource !== source ? (
    <img
      className={"model-brand-icon " + className}
      src={source}
      width={size}
      height={size}
      alt={decorative ? "" : name}
      aria-hidden={decorative || undefined}
      loading="lazy"
      decoding="async"
      onError={() => setFailedSource(source)}
    />
  ) : (
    <Cpu
      className={"model-brand-icon model-brand-fallback " + className}
      size={size}
      weight="regular"
      role={decorative ? undefined : "img"}
      aria-hidden={decorative || undefined}
      aria-label={decorative ? undefined : name + "（暂无品牌图标）"}
    />
  );
}
export default BrandIcon;
