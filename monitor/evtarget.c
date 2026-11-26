// Smallest possible program: exits immediately (no libc, static). Keeps exec cost low.
#if defined(__aarch64__)
void _start(void) { __asm__ volatile("mov x0, #0\n mov x8, #93\n svc #0"); }
#elif defined(__x86_64__)
void _start(void) { __asm__ volatile("mov $60, %eax\n xor %edi, %edi\n syscall"); }
#else
#error unsupported arch
#endif
