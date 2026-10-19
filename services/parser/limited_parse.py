import resource,runpy,sys
resource.setrlimit(resource.RLIMIT_CPU,(15,15))
resource.setrlimit(resource.RLIMIT_AS,(256*1024*1024,256*1024*1024))
resource.setrlimit(resource.RLIMIT_FSIZE,(12*1024*1024,12*1024*1024))
resource.setrlimit(resource.RLIMIT_NOFILE,(32,32))
runpy.run_path('/parser/parser.py',run_name='__main__')
