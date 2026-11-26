ARCH := $(shell uname -m | sed 's/x86_64/x86/;s/aarch64/arm64/')
BPFTOOL ?= bpftool
CFLAGS ?= -O2 -g -Wall

all: monitor evgen evtarget

vmlinux.h:
	$(BPFTOOL) btf dump file /sys/kernel/btf/vmlinux format c > $@ || (rm -f $@; false)

monitor.bpf.o: monitor.bpf.c monitor.h vmlinux.h
	clang -g -O2 -target bpf -D__TARGET_ARCH_$(ARCH) -I. -c $< -o $@

monitor.skel.h: monitor.bpf.o
	$(BPFTOOL) gen skeleton $< > $@

monitor: monitor.c monitor.h monitor.skel.h
	$(CC) $(CFLAGS) -I. $< -lbpf -lelf -lz -o $@

evgen: evgen.c
	$(CC) $(CFLAGS) $< -o $@

evtarget: evtarget.c
	$(CC) -O2 -static -nostdlib -fno-stack-protector -o $@ $<

clean:
	rm -f monitor evgen evtarget *.o monitor.skel.h vmlinux.h

.PHONY: all clean
