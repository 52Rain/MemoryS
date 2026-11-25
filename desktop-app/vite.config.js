import {defineConfig} from 'vite';
import {svelte} from '@sveltejs/vite-plugin-svelte';
export default defineConfig({plugins:[svelte()],base:'./',build:{lib:{entry:'src/main.js',name:'MemoryS',formats:['iife'],fileName:()=> 'app.js',cssFileName:'app'},target:'es2022',emptyOutDir:true}});
