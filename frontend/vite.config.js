import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import fs from 'fs';

// export default {
//   plugins: [react()],
//   server: {
//     https: {
//       key: fs.readFileSync('localhost-key.pem'),
//       cert: fs.readFileSync('localhost.pem'),
//     },
//     proxy: {
//       '/api': {
//         target: 'https://localhost:8000',
//         secure: false,
//       },
//     },
//   },
// };


export default defineConfig({
  plugins: [react()],
})
