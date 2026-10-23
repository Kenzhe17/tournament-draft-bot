import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'r!z3 Dashboard',
  description: 'Tournament Bot Dashboard',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
