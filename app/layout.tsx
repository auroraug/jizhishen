import type { Metadata } from "next";
import "./globals.css";
import "./audit-mvp.css";

export const metadata: Metadata = {
  title: "集智审｜农村集体建设工程全过程智能审查",
  description: "跨 OA、工程、三资、财务、招投标与档案系统的全过程智能审查工作台。",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
