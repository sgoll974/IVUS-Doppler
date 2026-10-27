.POSIX:

CFLAGS = -std=c99 -O2 -Wall -Wextra -pedantic

all: dd_udp_sender

dd_udp_sender: dd_udp_sender.c ../protocol.h
	$(CC) $(CFLAGS) -o $@ dd_udp_sender.c

clean:
	rm -f dd_udp_sender

.PHONY: all clean
