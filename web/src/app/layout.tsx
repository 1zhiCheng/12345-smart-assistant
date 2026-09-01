import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "芜湖政务 Agent · 12345热线工单智能辅助系统",
  description: "面向12345热线工作人员的诉求理解、工单生成、分类转派与回复辅助智能体",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
