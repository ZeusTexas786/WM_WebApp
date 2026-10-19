from django.core.files.uploadhandler import FileUploadHandler,StopUpload

class LimitedUploadHandler(FileUploadHandler):
    def __init__(self,request=None): super().__init__(request); self.total=0; self.file_bytes=0; self.files=0
    def new_file(self,*args,**kwargs):
        super().new_file(*args,**kwargs)
        self.file_bytes=0; self.files+=1
        if self.files>30: raise StopUpload(connection_reset=True)
    def receive_data_chunk(self,raw_data,start):
        self.file_bytes+=len(raw_data); self.total+=len(raw_data)
        if self.file_bytes>10*1024*1024 or self.total>300*1024*1024: raise StopUpload(connection_reset=True)
        return raw_data
    def file_complete(self,file_size): return None
